"""Rolling conversation summariser — LLM-based with extractive fallback."""

from __future__ import annotations

import textwrap
from typing import Sequence

from config import GOOGLE_API_KEY, GEMINI_MODEL, RotatingGenAIClient


# ── Summarisation prompt ─────────────────────────────────────────────────────

_SUMMARISE_PROMPT = textwrap.dedent("""\
You are a summariser for an AIOps Kubernetes monitoring chat agent.

Given a conversation between a user (SRE operator) and the agent, produce a
**concise rolling summary** that preserves:

1. **Key topics** discussed (which namespaces, pods, deployments, services).
2. **Findings & incidents** discovered (titles, severities, affected resources).
3. **Diagnoses & root causes** identified.
4. **Actions suggested** (kubectl commands, next steps the user was told).
5. **Open questions** or unresolved threads the user may return to.
6. **User preferences** expressed (e.g. "only show boutique namespace").

Rules:
- Be factual. Do NOT invent details that aren't in the conversation.
- Keep it under 400 words.
- Use bullet points for clarity.
- If there is an existing summary from earlier turns, merge the new information
  into it — do not discard old context.
- Output ONLY the summary, no preamble.
""")


# ── Public API ───────────────────────────────────────────────────────────────

def summarise_messages(
    messages: Sequence[dict],
    existing_summary: str = "",
) -> str:
    """
    Compress ``messages`` (list of {role, content} dicts) into a summary.

    If ``existing_summary`` is provided, the new summary merges both.

    Tries the LLM first; falls back to extractive heuristic on failure.
    """
    try:
        return _llm_summarise(messages, existing_summary)
    except Exception:
        return _extractive_fallback(messages, existing_summary)


# ── LLM strategy ────────────────────────────────────────────────────────────

def _llm_summarise(
    messages: Sequence[dict],
    existing_summary: str,
) -> str:
    """Call Gemini to produce a summary."""
    from google import genai
    from google.genai import types as gt

    client = RotatingGenAIClient()

    # Build the conversation text to summarise
    parts: list[str] = []
    if existing_summary:
        parts.append(f"## Previous summary\n{existing_summary}\n")
    parts.append("## New conversation turns to incorporate")
    for msg in messages:
        role_label = {"user": "User", "model": "Agent", "tool": "Tool"}.get(
            msg["role"], msg["role"]
        )
        content = msg["content"]
        # Truncate very long tool results
        if msg["role"] == "tool" and len(content) > 600:
            content = content[:600] + "…"
        parts.append(f"**{role_label}:** {content}")

    conversation_text = "\n\n".join(parts)

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            gt.Content(
                role="user",
                parts=[gt.Part.from_text(text=conversation_text)],
            )
        ],
        config=gt.GenerateContentConfig(
            system_instruction=_SUMMARISE_PROMPT,
            temperature=0.1,
            # No tools — pure text generation
        ),
    )

    candidate = response.candidates[0]
    text_parts = [p.text for p in (candidate.content.parts or []) if p.text]
    return "\n".join(text_parts).strip()


# ── Extractive fallback ─────────────────────────────────────────────────────

def _extractive_fallback(
    messages: Sequence[dict],
    existing_summary: str,
) -> str:
    """
    Simple heuristic: keep user questions and short model answers verbatim,
    compress tool results to one-line previews.
    """
    lines: list[str] = []
    if existing_summary:
        lines.append(f"Previous context:\n{existing_summary}\n")
    lines.append("Conversation continued:")
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if role == "user":
            lines.append(f"- User asked: {content[:200]}")
        elif role == "model":
            # Keep first 150 chars of agent answers
            lines.append(f"- Agent answered: {content[:150]}…" if len(content) > 150 else f"- Agent answered: {content}")
        elif role == "tool":
            lines.append(f"- [Tool data received]")
    return "\n".join(lines)
