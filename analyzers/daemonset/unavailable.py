"""
Detects DaemonSets with unavailable pods across nodes.

A DaemonSet ensures every (matching) node runs exactly one copy of a pod.
Common uses: Fluentd (log collection), Prometheus node-exporter, CNI plugins,
security agents, storage drivers.

If a DaemonSet pod is unavailable on a node, that node is effectively
unmonitored / missing its infrastructure agent. This is often silent in dashboards
because there's no "service degraded" alert — things just stop being collected.

Key fields:
  desired_number_scheduled: nodes that SHOULD run the pod (matching node selector)
  number_ready:             nodes where the pod is running and passing health checks
  number_unavailable:       desired - ready (the gap we care about)
  number_misscheduled:      nodes running the pod that SHOULDN'T be (rare — taints changed)
"""

from __future__ import annotations
from typing import Any
from analyzers.base import Analyzer, Finding, Severity


class DaemonSetUnavailableAnalyzer(Analyzer):

    @property
    def name(self) -> str:
        return "DaemonSetUnavailable"

    @property
    def supported_kinds(self) -> list[str]:
        return ["DaemonSet"]

    def analyze(self, snapshot: dict[str, Any]) -> list[Finding]:
        findings: list[Finding] = []
        meta = snapshot["metadata"]
        resource = f"{meta['namespace']}/{meta['name']}"
        status = snapshot.get("status") or {}

        desired      = status.get("desired_number_scheduled") or 0
        ready        = status.get("number_ready")             or 0
        unavailable  = status.get("number_unavailable")       or 0
        misscheduled = status.get("number_misscheduled")      or 0

        if desired == 0:
            return findings  # No matching nodes — not an error

        if unavailable > 0:
            severity = Severity.CRITICAL if ready == 0 else Severity.WARNING
            findings.append(Finding(
                analyzer=self.name,
                resource=resource,
                kind="DaemonSet",
                severity=severity,
                message=(
                    f"DaemonSet has {unavailable}/{desired} unavailable pods. "
                    f"Ready: {ready}/{desired} nodes. "
                    f"Affected nodes are missing this agent."
                ),
                reason="PodsUnavailable",
                raw_context={
                    "desired": desired,
                    "ready": ready,
                    "unavailable": unavailable,
                },
            ))

        if misscheduled > 0:
            findings.append(Finding(
                analyzer=self.name,
                resource=resource,
                kind="DaemonSet",
                severity=Severity.WARNING,
                message=(
                    f"DaemonSet has {misscheduled} misscheduled pod(s) — "
                    f"running on nodes that no longer match its node selector or toleration. "
                    f"This usually means node labels/taints were changed."
                ),
                reason="Misscheduled",
                raw_context={"misscheduled": misscheduled, "desired": desired},
            ))

        return findings
