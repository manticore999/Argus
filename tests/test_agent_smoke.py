"""Quick smoke test for the agent loop — not a full test suite."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.session import SessionStore
from agent.loop import AgentLoop


def main():
    store = SessionStore()
    sid = store.create_session("smoke-test")
    agent = AgentLoop(session_id=sid, session_store=store)

    questions = [
        "What are the active incidents?",
        "List all pods in the boutique namespace",
    ]

    for q in questions:
        print(f"\n{'='*60}")
        print(f"USER: {q}")
        print("="*60)
        for event in agent.chat(q):
            t = event["type"]
            if t == "tool_call":
                args_str = ", ".join(f"{k}={v!r}" for k, v in event["args"].items())
                print(f"  TOOL CALL: {event['name']}({args_str})")
            elif t == "tool_result":
                preview = event["result"][:200].replace("\n", " ")
                print(f"  RESULT:    {preview}...")
            elif t == "text":
                print(f"\n  AGENT ANSWER:\n{event['content'][:800]}")

    # Verify session persisted
    history = store.get_history(sid)
    print(f"\n--- Session has {len(history)} messages ---")
    for msg in history:
        role = msg["role"]
        content = msg["content"][:80]
        print(f"  [{role}] {content}")

    store.close()
    print("\nSmoke test complete.")


if __name__ == "__main__":
    main()
