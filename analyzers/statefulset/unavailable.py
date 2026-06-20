"""
Detects StatefulSets with unavailable or not-ready replicas.

StatefulSets differ from Deployments in an important way:
- Pods are named deterministically: mysql-0, mysql-1, mysql-2
- They scale sequentially: pod-N must be Ready before pod-N+1 starts
- If mysql-0 is stuck (e.g. can't mount PVC), the ENTIRE StatefulSet stalls

So even partial unavailability is significant — it can block ALL future scaling.
This is unlike Deployments where one bad replica just reduces capacity.
"""

from __future__ import annotations
from typing import Any
from analyzers.base import Analyzer, Finding, Severity


class StatefulSetUnavailableAnalyzer(Analyzer):

    @property
    def name(self) -> str:
        return "StatefulSetUnavailable"

    @property
    def supported_kinds(self) -> list[str]:
        return ["StatefulSet"]

    def analyze(self, snapshot: dict[str, Any]) -> list[Finding]:
        findings: list[Finding] = []
        meta = snapshot["metadata"]
        resource = f"{meta['namespace']}/{meta['name']}"
        spec   = snapshot.get("spec")   or {}
        status = snapshot.get("status") or {}

        desired         = spec.get("replicas", 1)
        ready           = status.get("ready_replicas")     or 0
        current         = status.get("current_replicas")   or 0
        updated         = status.get("updated_replicas")   or 0

        if desired == 0:
            return findings  # Intentionally scaled to zero

        # All replicas down
        if ready == 0 and desired > 0:
            findings.append(Finding(
                analyzer=self.name,
                resource=resource,
                kind="StatefulSet",
                severity=Severity.CRITICAL,
                message=(
                    f"StatefulSet has 0/{desired} ready replicas. "
                    f"Current: {current}. "
                    f"StatefulSets scale sequentially — if pod-0 is stuck, "
                    f"no other pods can start."
                ),
                reason="ZeroReadyReplicas",
                raw_context={"desired": desired, "ready": ready, "current": current},
            ))
            return findings

        # Partial — some pods not ready
        if ready < desired:
            missing = desired - ready
            findings.append(Finding(
                analyzer=self.name,
                resource=resource,
                kind="StatefulSet",
                severity=Severity.WARNING,
                message=(
                    f"StatefulSet has {ready}/{desired} ready replicas "
                    f"({missing} unavailable). Updated: {updated}."
                ),
                reason="PartiallyAvailable",
                raw_context={"desired": desired, "ready": ready, "missing": missing},
            ))

        return findings
