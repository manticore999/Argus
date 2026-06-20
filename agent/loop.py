"""Core conversation loop — Gemini function-calling agent."""

from __future__ import annotations

import json
import re
import time
from typing import Generator

from google import genai
from google.genai import types as gt

from config import GOOGLE_API_KEY, GEMINI_MODEL, RotatingGenAIClient
from agent.system_prompt import SYSTEM_PROMPT
from agent.tools import get_declarations, get_executor
from agent.session import SessionStore, SUMMARY_THRESHOLD, RECENT_WINDOW
from agent.summariser import summarise_messages


MAX_TOOL_ROUNDS = 10  # safety limit on consecutive tool-call rounds
MAX_RETRIES = 3       # retries on 429 rate-limit errors
BASE_BACKOFF = 15     # seconds — initial wait before retry


class AgentLoop:
    """Stateful conversation agent backed by Gemini function-calling."""

    def __init__(
        self,
        session_id: str,
        session_store: SessionStore | None = None,
        model: str = GEMINI_MODEL,
    ) -> None:
        self.session_id = session_id
        self.model = model
        self._store = session_store or SessionStore()
        self._executor = get_executor()

        self._client = RotatingGenAIClient()
        self._config = gt.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[gt.Tool(function_declarations=get_declarations())],
            temperature=0.2,
        )

        self._contents: list[gt.Content] = self._load_history()

    # ── Public API ───────────────────────────────────────────────────────────

    def chat(self, user_message: str) -> Generator[dict, None, None]:
        """Send a user message, yield tool_call/tool_result/text events."""
        # Append user turn
        user_content = gt.Content(
            role="user",
            parts=[gt.Part.from_text(text=user_message)],
        )
        self._contents.append(user_content)
        self._store.append_message(self.session_id, "user", user_message)

        for _ in range(MAX_TOOL_ROUNDS):
            response = self._call_gemini()

            candidate = response.candidates[0]
            model_content = candidate.content
            self._contents.append(model_content)

            # Separate function calls from text parts
            parts = model_content.parts or []
            fn_calls = [p for p in parts if p.function_call]
            text_parts = [p for p in parts if p.text]

            if not fn_calls:
                # No tool calls → final text answer
                answer = "\n".join(p.text for p in text_parts if p.text)
                self._store.append_message(self.session_id, "model", answer)
                yield {"type": "text", "content": answer}
                # Trigger background summarisation if history is long
                self._maybe_summarise()
                return

            # Process each function call
            fn_response_parts: list[gt.Part] = []

            for part in fn_calls:
                fc = part.function_call
                tool_name = fc.name
                tool_args = dict(fc.args) if fc.args else {}

                yield {"type": "tool_call", "name": tool_name, "args": tool_args}

                # Execute the tool
                executor_fn = self._executor.get(tool_name)
                if executor_fn:
                    try:
                        result = executor_fn(**tool_args)
                    except Exception as e:
                        result = f"Tool error: {e}"
                else:
                    result = f"Unknown tool: {tool_name}"

                yield {"type": "tool_result", "name": tool_name, "result": result}

                fn_response_parts.append(
                    gt.Part.from_function_response(
                        name=tool_name,
                        response={"result": result},
                    )
                )

            # Persist tool interaction as a summary
            tool_summary = [
                {"tool": p.function_response.name, "result_preview": _truncate(p.function_response.response.get("result", ""), 200)}
                for p in fn_response_parts
            ]
            self._store.append_message(
                self.session_id, "tool",
                json.dumps(tool_summary),
            )

            # Append function responses as a user turn (Gemini convention)
            self._contents.append(
                gt.Content(role="user", parts=fn_response_parts)
            )

        # Safety: if we exhausted tool rounds
        fallback = "I reached the maximum number of tool calls. Here's what I found so far — please refine your question."
        self._store.append_message(self.session_id, "model", fallback)
        yield {"type": "text", "content": fallback}
        self._maybe_summarise()

    # ── Gemini call with rate-limit retry ───────────────────────────────────

    def _call_gemini(self):
        """Call Gemini with retry on 429 rate-limit errors."""
        for attempt in range(MAX_RETRIES + 1):
            try:
                return self._client.models.generate_content(
                    model=self.model,
                    contents=self._contents,
                    config=self._config,
                )
            except Exception as e:
                err_str = str(e)
                if "429" in err_str and "RESOURCE_EXHAUSTED" in err_str:
                    if attempt >= MAX_RETRIES:
                        raise
                    # Try to extract retry delay from error
                    wait = BASE_BACKOFF * (attempt + 1)
                    match = re.search(r'retryDelay.*?(\d+)', err_str)
                    if match:
                        wait = max(int(match.group(1)) + 2, wait)
                    print(f"  [rate-limit] Waiting {wait}s before retry {attempt + 1}/{MAX_RETRIES}...")
                    time.sleep(wait)
                else:
                    raise

    # ── History loading ──────────────────────────────────────────────────────

    def _load_history(self) -> list[gt.Content]:
        """Rebuild Content objects from DB: summary (if any) + recent raw messages."""
        contents: list[gt.Content] = []

        # Inject rolling summary
        summary = self._store.get_summary(self.session_id)
        if summary:
            contents.append(gt.Content(
                role="user",
                parts=[gt.Part.from_text(
                    text=(
                        "[CONVERSATION CONTEXT — rolling summary of earlier turns]\n"
                        f"{summary}\n"
                        "[END SUMMARY — recent messages follow]"
                    )
                )],
            ))
            # Ack from model to maintain alternating roles
            contents.append(gt.Content(
                role="model",
                parts=[gt.Part.from_text(
                    text="Understood. I have the prior context. Let's continue."
                )],
            ))

        # Recent raw messages
        raw = self._store.get_history(self.session_id)
        for msg in raw:
            role = msg["role"]
            text = msg["content"]

            if role == "user":
                contents.append(gt.Content(
                    role="user",
                    parts=[gt.Part.from_text(text=text)],
                ))
            elif role == "model":
                contents.append(gt.Content(
                    role="model",
                    parts=[gt.Part.from_text(text=text)],
                ))
            elif role == "tool":
                # Summarise tool interactions as a model message for context
                contents.append(gt.Content(
                    role="model",
                    parts=[gt.Part.from_text(
                        text=f"[Tool results: {text[:300]}]"
                    )],
                ))

        return contents

    # ── Rolling summarisation ────────────────────────────────────────────────

    def _maybe_summarise(self) -> None:
        """Compress old messages into a rolling summary when count exceeds threshold."""
        count = self._store.message_count(self.session_id)
        if count <= SUMMARY_THRESHOLD:
            return

        # How many messages to compress (everything beyond RECENT_WINDOW)
        to_compress = count - RECENT_WINDOW
        if to_compress <= 0:
            return

        old_msgs = self._store.get_oldest_messages(self.session_id, to_compress)
        if not old_msgs:
            return

        existing_summary = self._store.get_summary(self.session_id)

        # Call the summariser (LLM with extractive fallback)
        try:
            new_summary = summarise_messages(
                messages=[{"role": m["role"], "content": m["content"]} for m in old_msgs],
                existing_summary=existing_summary,
            )
        except Exception:
            # If everything fails, just do a naive trim
            new_summary = existing_summary or "(prior context lost)"

        # Persist the updated summary
        self._store.update_summary(self.session_id, new_summary)

        # Prune the compressed messages from the DB
        max_id = old_msgs[-1]["id"]
        self._store.delete_messages_up_to(self.session_id, max_id)

        # Rebuild in-memory contents from the new state
        self._contents = self._load_history()


def _truncate(s: str, max_len: int) -> str:
    return s[:max_len] + "…" if len(s) > max_len else s
