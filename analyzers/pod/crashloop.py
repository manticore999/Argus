"""
Detects pods stuck in CrashLoopBackOff.

Reference: K8sGPT pkg/analyzer/pod.go — CrashLoopBackOff branch.

A container enters CrashLoopBackOff when it repeatedly crashes and
kubelet backs off restarting it. The waiting reason is set to
"CrashLoopBackOff" and the last termination state contains the
exit reason (e.g. OOMKilled, Error).
"""

from __future__ import annotations

from typing import Any

from analyzers.base import Analyzer, Finding, Severity


class PodCrashLoopAnalyzer(Analyzer):

    @property
    def name(self) -> str:
        return "PodCrashLoopBackOff"

    @property
    def supported_kinds(self) -> list[str]:
        return ["Pod"]

    def analyze(self, snapshot: dict[str, Any]) -> list[Finding]:
        findings: list[Finding] = []
        meta = snapshot["metadata"]
        resource = f"{meta['namespace']}/{meta['name']}"
        status = (snapshot.get("status") or {})
        all_statuses = (
            (status.get("container_statuses") or [])
            + (status.get("init_container_statuses") or [])
        )

        for cs in all_statuses:
            state = (cs.get("state") or {})
            waiting = (state.get("waiting") or {})
            if waiting.get("reason") != "CrashLoopBackOff":
                continue

            # Pull last termination reason if available (mirrors K8sGPT).
            last_state = (cs.get("last_state") or {})
            terminated = (last_state.get("terminated") or {})
            last_reason = terminated.get("reason", "Unknown")
            container_name = cs.get("name", "unknown")
            restart_count = cs.get("restart_count", 0)

            findings.append(Finding(
                analyzer=self.name,
                resource=resource,
                kind="Pod",
                severity=Severity.CRITICAL,
                message=(
                    f"Container '{container_name}' is in CrashLoopBackOff. "
                    f"Last termination reason: {last_reason}. "
                    f"Restart count: {restart_count}."
                ),
                reason="CrashLoopBackOff",
                raw_context={
                    "container": container_name,
                    "last_termination_reason": last_reason,
                    "restart_count": restart_count,
                },
            ))

        return findings
