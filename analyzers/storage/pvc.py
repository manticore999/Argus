"""
PVC (PersistentVolumeClaim) Analyzer - Detects storage provisioning issues.
Enables: Storage → Pod correlation (Rule 5).
"""

from typing import Any
from analyzers.base import Analyzer, Finding, Severity


class PVCAnalyzer(Analyzer):
    """
    Detects PVC provisioning failures:
    - PVC stuck in Pending (storage class issues, quota, no PV available)
    
    This is a DOMINANT analyzer - enables correlation engine to suppress
    pod FailedMount errors when root cause is PVC provisioning failure.
    """

    @property
    def name(self) -> str:
        return "PVCAnalyzer"

    @property
    def supported_kinds(self) -> list[str]:
        return ["PersistentVolumeClaim"]

    def analyze(self, snapshot: dict[str, Any]) -> list[Finding]:
        findings: list[Finding] = []
        meta = snapshot.get("metadata", {})
        namespace = meta.get("namespace", "default")
        name = meta.get("name", "unknown")
        resource = f"{namespace}/{name}"
        
        status = snapshot.get("status", {})
        phase = status.get("phase", "")
        
        if phase == "Pending":
            # PVC stuck in Pending - storage provisioning issue
            conditions = status.get("conditions", [])
            condition_messages = []
            
            for cond in conditions:
                if cond.get("status") == "False":
                    msg = cond.get("message", "")
                    if msg:
                        condition_messages.append(msg)
            
            # Build message
            if condition_messages:
                message = f"PVC '{resource}' is stuck in Pending. {' '.join(condition_messages)}"
            else:
                message = f"PVC '{resource}' is stuck in Pending (waiting for storage provisioning)."
            
            findings.append(Finding(
                analyzer=self.name,
                resource=resource,
                kind="PersistentVolumeClaim",
                severity=Severity.CRITICAL,
                message=message,
                reason="PVCPending",
                raw_context={
                    "phase": phase,
                    "conditions": conditions,
                    "storage_class": snapshot.get("spec", {}).get("storageClassName", ""),
                    "requested_storage": snapshot.get("spec", {}).get("resources", {}).get("requests", {}).get("storage", "")
                }
            ))
        
        return findings
