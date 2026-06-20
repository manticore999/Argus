"""
Volume Mount Analyzer - Detects volume mount failures in pods.
Enables: Storage → Pod correlation (Rule 5).
"""

import re
from typing import Any
from analyzers.base import Analyzer, Finding, Severity


class VolumeMountAnalyzer(Analyzer):
    """
    Detects pod volume mount failures:
    - FailedMount (cannot mount volume)
    - FailedAttachVolume (cannot attach to node)
    - VolumeAttachFailure
    
    Works with PVCAnalyzer - if PVC is pending, these are symptoms.
    Correlation engine links pod mount failures to PVC provisioning issues.
    """

    @property
    def name(self) -> str:
        return "VolumeMountAnalyzer"

    @property
    def supported_kinds(self) -> list[str]:
        return ["Pod"]

    def analyze(self, snapshot: dict[str, Any]) -> list[Finding]:
        findings: list[Finding] = []
        meta = snapshot.get("metadata", {})
        namespace = meta.get("namespace", "default")
        name = meta.get("name", "unknown")
        resource = f"{namespace}/{name}"
        
        # Check events for volume-related failures
        events = snapshot.get("events", [])
        
        for evt in events:
            reason = evt.get("reason", "")
            
            if reason in ["FailedMount", "FailedAttachVolume", "VolumeAttachFailure"]:
                message = evt.get("message", "")
                
                # Extract PVC name from event message
                pvc_name = self._extract_pvc_from_message(message)
                
                findings.append(Finding(
                    analyzer=self.name,
                    resource=resource,
                    kind="Pod",
                    severity=Severity.CRITICAL,
                    message=f"Pod '{resource}' cannot mount volume. {message}",
                    reason=reason,
                    raw_context={
                        "pvc_name": pvc_name,
                        "event_message": message,
                        "event_reason": reason
                    }
                ))
                
                # Only report first volume failure event to avoid spam
                break
        
        return findings
    
    def _extract_pvc_from_message(self, message: str) -> str:
        """
        Parse event message to extract PVC name.
        
        Example messages:
        - "Unable to attach or mount volumes: failed to mount volume 'data-volume': ..."
        - "MountVolume.SetUp failed for volume 'pvc-abc123' : ..."
        """
        
        match = re.search(r"volume ['\"]([^'\"]+)['\"]", message)
        if match:
            return match.group(1)
        
        
        match = re.search(r"pvc-[a-z0-9\-]+", message)
        if match:
            return match.group(0)
        
        return "unknown"
