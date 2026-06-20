"""
Detects deployments with unavailable or zero-ready replicas.

Reference: K8sGPT pkg/analyzer/deployment.go

A Deployment is unhealthy when:
- spec.replicas > 0 but status.available_replicas == 0
- status.unavailable_replicas > 0
- A condition of type "Available" has status "False"
"""

from __future__ import annotations

from typing import Any

from analyzers.base import Analyzer, Finding, Severity


class DeploymentUnavailableAnalyzer(Analyzer):

    @property
    def name(self) -> str:
        return "DeploymentUnavailable"

    @property
    def supported_kinds(self) -> list[str]:
        return ["Deployment"]

    def analyze(self, snapshot: dict[str, Any]) -> list[Finding]:
        findings: list[Finding] = []
        meta = snapshot["metadata"]
        resource = f"{meta['namespace']}/{meta['name']}"
        spec = (snapshot.get("spec") or {})
        status = (snapshot.get("status") or {})

        desired = spec.get("replicas", 1)
        available = status.get("available_replicas") or 0
        unavailable = status.get("unavailable_replicas") or 0
        ready = status.get("ready_replicas") or 0

        # Nothing to check if desired is 0 (scaled down intentionally).
        if desired == 0:
            return findings

        # Check for zero available replicas.
        if available == 0 and desired > 0:
            findings.append(Finding(
                analyzer=self.name,
                resource=resource,
                kind="Deployment",
                severity=Severity.CRITICAL,
                message=(
                    f"Deployment has 0/{desired} available replicas. "
                    f"Ready: {ready}, Unavailable: {unavailable}."
                ),
                reason="ZeroAvailableReplicas",
                raw_context={
                    "desired": desired,
                    "available": available,
                    "ready": ready,
                    "unavailable": unavailable,
                },
            ))
            return findings

        # Check for partial unavailability.
        if unavailable > 0:
            findings.append(Finding(
                analyzer=self.name,
                resource=resource,
                kind="Deployment",
                severity=Severity.WARNING,
                message=(
                    f"Deployment has {unavailable} unavailable replica(s). "
                    f"Desired: {desired}, Available: {available}."
                ),
                reason="UnavailableReplicas",
                raw_context={
                    "desired": desired,
                    "available": available,
                    "ready": ready,
                    "unavailable": unavailable,
                },
            ))

        # Check conditions for explicit "Available=False".
        for cond in (status.get("conditions") or []):
            if cond.get("type") == "Available" and cond.get("status") == "False":
                msg = cond.get("message", "")
                if not findings:  # Avoid duplicate if already caught above.
                    findings.append(Finding(
                        analyzer=self.name,
                        resource=resource,
                        kind="Deployment",
                        severity=Severity.CRITICAL,
                        message=f"Deployment condition Available=False. {msg}",
                        reason="AvailableFalse",
                        raw_context={"condition_message": msg},
                    ))

        return findings
