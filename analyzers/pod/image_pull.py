"""
Detects pods stuck in ImagePullBackOff or ErrImagePull.

Reference: K8sGPT pkg/analyzer/pod.go — isErrorReason() for image errors.

These occur when kubelet cannot pull the container image. Common causes:
- Typo in image name
- Image does not exist in registry
- Missing image pull secrets
- Private registry auth failure
"""

from __future__ import annotations

from typing import Any

from analyzers.base import Analyzer, Finding, Severity

_IMAGE_PULL_REASONS = frozenset({
    "ImagePullBackOff",
    "ErrImagePull",
    "ErrImageNeverPull",
    "InvalidImageName",
    "ImageInspectError",
})


class PodImagePullAnalyzer(Analyzer):

    @property
    def name(self) -> str:
        return "PodImagePullBackOff"

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
            reason = waiting.get("reason", "")
            if reason not in _IMAGE_PULL_REASONS:
                continue

            container_name = cs.get("name", "unknown")
            message_detail = waiting.get("message", "")

            findings.append(Finding(
                analyzer=self.name,
                resource=resource,
                kind="Pod",
                severity=Severity.CRITICAL,
                message=(
                    f"Container '{container_name}' cannot pull image: {reason}."
                    + (f" {message_detail}" if message_detail else "")
                ),
                reason=reason,
                raw_context={
                    "container": container_name,
                    "waiting_message": message_detail,
                },
            ))

        return findings
