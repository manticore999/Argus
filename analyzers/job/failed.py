"""Detects permanently failed Kubernetes Jobs."""

from __future__ import annotations
from typing import Any
from analyzers.base import Analyzer, Finding, Severity


class JobFailureAnalyzer(Analyzer):

    @property
    def name(self) -> str:
        return "JobFailed"

    @property
    def supported_kinds(self) -> list[str]:
        return ["Job"]

    def analyze(self, snapshot: dict[str, Any]) -> list[Finding]:
        findings: list[Finding] = []
        meta = snapshot["metadata"]
        # Jobs are namespaced
        namespace = meta.get("namespace") or "default"
        resource = f"{namespace}/{meta['name']}"
        status     = snapshot.get("status")     or {}
        conditions = status.get("conditions")   or []
        failed     = status.get("failed")       or 0
        succeeded  = status.get("succeeded")    or 0

        for cond in conditions:
            cond_type   = cond.get("type", "")
            cond_status = cond.get("status", "")
            reason      = cond.get("reason", "")
            cond_msg    = cond.get("message", "")

            # Only care about active failure conditions
            if cond_type != "Failed" or cond_status != "True":
                continue

            if reason == "BackoffLimitExceeded":
                findings.append(Finding(
                    analyzer=self.name,
                    resource=resource,
                    kind="Job",
                    severity=Severity.CRITICAL,
                    message=(
                        f"Job permanently failed after {failed} attempt(s). "
                        f"BackoffLimit reached — k8s will not retry. "
                        f"Check pod logs for the root cause. {cond_msg}"
                    ),
                    reason="BackoffLimitExceeded",
                    raw_context={"failed_attempts": failed, "succeeded": succeeded},
                ))

            elif reason == "DeadlineExceeded":
                spec = snapshot.get("spec") or {}
                deadline = spec.get("active_deadline_seconds", "unknown")
                findings.append(Finding(
                    analyzer=self.name,
                    resource=resource,
                    kind="Job",
                    severity=Severity.CRITICAL,
                    message=(
                        f"Job exceeded its deadline of {deadline}s and was terminated. "
                        f"The task did not complete within the allowed time window."
                    ),
                    reason="DeadlineExceeded",
                    raw_context={
                        "failed_attempts": failed,
                        "active_deadline_seconds": deadline,
                    },
                ))

            else:
                # Unknown failure condition — still worth surfacing
                findings.append(Finding(
                    analyzer=self.name,
                    resource=resource,
                    kind="Job",
                    severity=Severity.WARNING,
                    message=f"Job failed: {reason}. {cond_msg}",
                    reason=reason or "JobFailed",
                    raw_context={"failed_attempts": failed, "condition_message": cond_msg},
                ))

        return findings
