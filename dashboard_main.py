"""
Dashboard-integrated AIOps Agent.

Runs the React dashboard (port 8080) + K8s event watcher in parallel threads.

DashboardAIOpsAgent only overrides the four observable hooks declared in
AIOpsAgent — no pipeline logic is duplicated here.
"""

import asyncio
import threading
import time

import uvicorn

from dashboard import app, push_event
from main import AIOpsAgent
from engine.agent_graph import AgentGraph


class DashboardAIOpsAgent(AIOpsAgent):
    """
    AIOps agent that pushes every pipeline stage to the live dashboard via SSE.
    Adds LLM diagnosis 30 s after each new incident is created.
    """

    def __init__(self) -> None:
        super().__init__()
        self.agent = AgentGraph()
        if self.agent._client:
            print("[LLM] Gemini available — incidents will be diagnosed automatically")
        else:
            print("[LLM] No API key found — set GOOGLE_API_KEY to enable LLM diagnosis")

    # ── Observable hooks ──────────────────────────────────────────────────────

    def on_k8s_event(self, kind: str, namespace: str, name: str,
                     reason: str, message: str) -> None:
        ns_name = f"{namespace}/{name}" if namespace else name
        push_event("k8s_event", f"{kind} {ns_name} — {reason}: {message[:90]}")

    def on_finding(self, finding) -> None:
        push_event(
            "finding",
            f"{finding.severity.name} | {finding.kind}/{finding.resource}"
            f" — {finding.reason} [{finding.analyzer}]",
        )

    def on_correlation(self, suppressed_count: int) -> None:
        push_event(
            "correlation",
            f"Correlation rule applied — {suppressed_count} finding(s) suppressed as symptoms",
        )

    def on_incident(self, incident_id: str, scope: str,
                    resource: str, title: str) -> None:
        push_event("incident", f"INCIDENT: {scope}/{resource} — {title}")
        if self.agent._client:
            t = threading.Timer(30, self._run_agentic_triage, args=[incident_id, title, scope, resource])
            t.daemon = True
            t.start()

    # ── LLM diagnosis ─────────────────────────────────────────────────────────

    def _run_agentic_triage(self, incident_id: str, title: str, scope: str, resource: str) -> None:
        """
        Runs the 3-step AgentGraph loop to diagnose and build a remediation plan.
        """
        try:
            # We reconstruct a basic incident dict for the prompt since AgentGraph expects it
            incident = {
                "id": incident_id,
                "title": title,
                "scope": scope,
                "severity": "High", # Assuming high for active incident
                "resource": resource
            }
            plan = self.agent.run_triage(incident)
            if plan:
                self.incident_store.save_diagnosis(incident_id, plan)
                
                # We save the plan as a diagnosis string and also store suggested actions if needed
                # (For simplicity here, we adapt it to fit the existing UI push mechanism)
                push_event(
                    "incident",
                    f"Agent Triage: {plan.root_cause} [confidence: {plan.confidence}]"
                )
                if plan.suggested_actions:
                    for action in plan.suggested_actions:
                        push_event(
                            "incident",
                            f"Proposed Action: {action.action_tool_name} -> {action.explanation} (Requires Approval)"
                        )
        except Exception as e:
            print(f"[Agent] Triage thread error: {e}")


# ── Dashboard server thread ───────────────────────────────────────────────────

def run_dashboard(port: int) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


def main() -> None:
    port = 8080
    print("=" * 60)
    print("  AIOps Dashboard + Event Watcher")
    print("=" * 60)

    agent = DashboardAIOpsAgent()

    t = threading.Thread(target=run_dashboard, args=(port,), daemon=True)
    t.start()
    time.sleep(2)

    print(f"\n  Dashboard: http://localhost:{port}")
    print("=" * 60)

    agent.run()


if __name__ == "__main__":
    main()
