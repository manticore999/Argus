"""
Detects pods stuck in Pending state (unschedulable or resource pressure).

Reference: K8sGPT pkg/analyzer/pod.go — Pending phase + Unschedulable condition.

A pod stays Pending when the scheduler cannot place it. Common causes:
- Insufficient CPU/memory on nodes
- Node affinity/selector mismatch
- Taints without tolerations
- PVC not bound
"""

from __future__ import annotations

from typing import Any

from analyzers.base import Analyzer, Finding, Severity


class PodPendingAnalyzer(Analyzer):

    @property
    def name(self) -> str:
        return "PodPending"

    @property
    def supported_kinds(self) -> list[str]:
        return ["Pod"]

    def analyze(self, snapshot: dict[str, Any]) -> list[Finding]:
        findings: list[Finding] = []
        meta = snapshot["metadata"]
        resource = f"{meta['namespace']}/{meta['name']}"
        status = (snapshot.get("status") or {})

        if status.get("phase") != "Pending":
            return findings

        # Check conditions for Unschedulable (mirrors K8sGPT).
        for cond in (status.get("conditions") or []):
            if (
                cond.get("type") == "PodScheduled"
                and cond.get("reason") == "Unschedulable"
            ):
                message = cond.get("message", "No details available.")
                findings.append(Finding(
                    analyzer=self.name,
                    resource=resource,
                    kind="Pod",
                    severity=Severity.CRITICAL,
                    message=f"Pod is Pending (Unschedulable). {message}",
                    reason="Unschedulable",
                    raw_context={"condition_message": message},
                ))
                return findings  # One finding is enough for this pattern.

        # Check for containers stuck in ContainerCreating with error events.
        for cs in (status.get("container_statuses") or []):
            state = (cs.get("state") or {})
            waiting = (state.get("waiting") or {})
            reason = waiting.get("reason", "")
            if reason == "ContainerCreating":
                # Look for error events attached to the snapshot.
                for evt in (snapshot.get("events") or []):
                    if evt.get("type") == "Warning" and evt.get("message"):
                        findings.append(Finding(
                            analyzer=self.name,
                            resource=resource,
                            kind="Pod",
                            severity=Severity.WARNING,
                            message=(
                                f"Pod is Pending (ContainerCreating). "
                                f"Event: {evt.get('message', '')}"
                            ),
                            reason=evt.get("reason", "ContainerCreating"),
                            raw_context={
                                "container": cs.get("name", "unknown"),
                                "event_reason": evt.get("reason", ""),
                                "event_message": evt.get("message", ""),
                            },
                        ))
                        return findings

        # Generic Pending — no specific reason found.
        if not findings:
            findings.append(Finding(
                analyzer=self.name,
                resource=resource,
                kind="Pod",
                severity=Severity.WARNING,
                message="Pod is Pending with no further detail from conditions or events.",
                reason="Pending",
                raw_context={},
            ))

        return findings
