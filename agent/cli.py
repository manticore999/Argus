"""Interactive CLI for the AIOps chat agent."""

from __future__ import annotations

import os

from agent.loop import AgentLoop
from agent.session import SessionStore
from agent.tools import get_declarations

_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_CYAN   = "\033[36m"
_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_RED    = "\033[31m"
_BLUE   = "\033[34m"


def _print_banner() -> None:
    print(f"""
{_BOLD}{_CYAN}╔══════════════════════════════════════════════════════════╗
║              AIOps Kubernetes Agent                       ║
║     Interactive cluster diagnostics powered by Gemini     ║
╚══════════════════════════════════════════════════════════╝{_RESET}

{_DIM}Commands:  /new  /sessions  /switch <id>  /history  /tools  /quit{_RESET}
""")


def _print_tool_call(name: str, args: dict) -> None:
    args_str = ", ".join(f"{k}={v!r}" for k, v in args.items()) if args else ""
    print(f"  {_DIM}{_YELLOW}> {name}({args_str}){_RESET}")


def _print_tool_result(name: str, result: str) -> None:
    # Show a truncated preview
    preview = result[:200].replace("\n", " ")
    if len(result) > 200:
        preview += "…"
    print(f"  {_DIM}{_BLUE}↳  {preview}{_RESET}")


def _print_agent_text(text: str) -> None:
    print(f"\n{_GREEN}{_BOLD}Agent:{_RESET} {text}\n")


def _print_error(msg: str) -> None:
    print(f"{_RED}Error: {msg}{_RESET}")


class CLI:
    """Interactive Read-Eval-Print Loop for the AIOps agent."""

    def __init__(self) -> None:
        self._session_store = SessionStore()
        self._session_id: str | None = None
        self._agent: AgentLoop | None = None

    def run(self) -> None:
        _print_banner()
        self._ensure_session()

        while True:
            try:
                user_input = input(f"{_BOLD}You:{_RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                break

            if not user_input:
                continue

            if user_input.startswith("/"):
                if self._handle_command(user_input):
                    continue
                else:
                    break  # /quit
            else:
                self._send_message(user_input)

    # ── Commands ─────────────────────────────────────────────────────────────

    def _handle_command(self, cmd: str) -> bool:
        """Handle a slash command. Returns False if the CLI should exit."""
        parts = cmd.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if command == "/quit":
            print("Bye!")
            return False

        elif command == "/new":
            self._new_session(title=arg)

        elif command == "/sessions":
            self._list_sessions()

        elif command == "/switch":
            self._switch_session(arg)

        elif command == "/history":
            self._show_history()

        elif command == "/tools":
            self._list_tools()

        else:
            print(f"{_DIM}Unknown command: {command}. "
                  f"Available: /new /sessions /switch /history /tools /quit{_RESET}")

        return True

    def _new_session(self, title: str = "") -> None:
        self._session_id = self._session_store.create_session(title=title or "Chat session")
        self._agent = AgentLoop(
            session_id=self._session_id,
            session_store=self._session_store,
        )
        print(f"{_DIM}New session: {self._session_id[:12]}…{_RESET}\n")

    def _list_sessions(self) -> None:
        sessions = self._session_store.list_sessions()
        if not sessions:
            print(f"{_DIM}No sessions yet.{_RESET}")
            return
        print(f"\n{_BOLD}Sessions:{_RESET}")
        for s in sessions:
            marker = " ← current" if s["id"] == self._session_id else ""
            title = s["title"] or "(untitled)"
            print(f"  {s['id'][:12]}…  {title}  {_DIM}{s['updated_at']}{marker}{_RESET}")
        print()

    def _switch_session(self, session_id_prefix: str) -> None:
        if not session_id_prefix:
            _print_error("Usage: /switch <session-id-prefix>")
            return
        # Find session by prefix match
        sessions = self._session_store.list_sessions(limit=100)
        matches = [s for s in sessions if s["id"].startswith(session_id_prefix)]
        if len(matches) == 0:
            _print_error(f"No session matching '{session_id_prefix}'")
        elif len(matches) > 1:
            _print_error(f"Ambiguous prefix — {len(matches)} matches. Be more specific.")
        else:
            self._session_id = matches[0]["id"]
            self._agent = AgentLoop(
                session_id=self._session_id,
                session_store=self._session_store,
            )
            print(f"{_DIM}Switched to session: {self._session_id[:12]}… "
                  f"({matches[0]['title']}){_RESET}\n")

    def _show_history(self) -> None:
        if not self._session_id:
            _print_error("No active session.")
            return
        history = self._session_store.get_history(self._session_id)
        if not history:
            print(f"{_DIM}No messages in this session yet.{_RESET}")
            return
        print(f"\n{_BOLD}Session history ({len(history)} messages):{_RESET}")
        for msg in history:
            role = msg["role"]
            content = msg["content"][:200]
            if role == "user":
                print(f"  {_BOLD}You:{_RESET} {content}")
            elif role == "model":
                print(f"  {_GREEN}Agent:{_RESET} {content}")
            elif role == "tool":
                print(f"  {_DIM}[tools]{_RESET} {content[:100]}")
        print()

    def _list_tools(self) -> None:
        decls = get_declarations()
        print(f"\n{_BOLD}Available tools ({len(decls)}):{_RESET}")
        for d in decls:
            params = ""
            if d.parameters and d.parameters.get("properties"):
                params = ", ".join(d.parameters["properties"].keys())
            print(f"  {_YELLOW}{d.name}{_RESET}({params})")
            print(f"    {_DIM}{d.description[:100]}{_RESET}")
        print()

    # ── Chat ─────────────────────────────────────────────────────────────────

    def _ensure_session(self) -> None:
        """Make sure there's an active session, create one if not."""
        if self._session_id is None:
            self._new_session()

    def _send_message(self, message: str) -> None:
        """Send a message through the agent loop and display events."""
        if self._agent is None:
            self._ensure_session()

        try:
            for event in self._agent.chat(message):
                etype = event["type"]
                if etype == "tool_call":
                    _print_tool_call(event["name"], event["args"])
                elif etype == "tool_result":
                    _print_tool_result(event["name"], event["result"])
                elif etype == "text":
                    _print_agent_text(event["content"])
        except KeyboardInterrupt:
            print(f"\n{_DIM}(interrupted){_RESET}")
        except Exception as e:
            _print_error(str(e))


def main() -> None:
    cli = CLI()
    cli.run()


if __name__ == "__main__":
    main()
