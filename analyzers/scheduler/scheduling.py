"""
Scheduler Failure Analyzer - Detects pod scheduling failures.
Enables: Cluster resource exhaustion detection (Rule 1b, Rule 3).
"""

from typing import Any
from analyzers.base import Analyzer, Finding, Severity


class SchedulerFailureAnalyzer(Analyzer):
    """
    Detects scheduler failures when pods cannot be scheduled:
    - FailedScheduling (no nodes available, resource constraints)
    - InsufficientCPU, InsufficientMemory
    - Node affinity mismatches
    - Taint/toleration issues
    
    Enables namespace-level grouping: if 10 pods can't schedule due to
    "Insufficient memory", correlation engine groups into cluster exhaustion incident.
    """

    @property
    def name(self) -> str:
        return "SchedulerFailureAnalyzer"

    @property
    def supported_kinds(self) -> list[str]:
        return ["Pod"]

    def analyze(self, snapshot: dict[str, Any]) -> list[Finding]:
        findings: list[Finding] = []
        meta = snapshot.get("metadata", {})
        namespace = meta.get("namespace", "default")
        name = meta.get("name", "unknown")
        resource = f"{namespace}/{name}"
        
        status = snapshot.get("status", {})
        phase = status.get("phase", "")
        
        # Only check Pending pods
        if phase != "Pending":
            return findings
        
        # Look for FailedScheduling events
        events = snapshot.get("events", [])
        
        for evt in events:
            if evt.get("reason") == "FailedScheduling":
                message = evt.get("message", "")
                
                # Parse structured scheduler reason
                scheduler_reason = self._parse_scheduler_reason(message)
                
                findings.append(Finding(
                    analyzer=self.name,
                    resource=resource,
                    kind="Pod",
                    severity=Severity.WARNING,
                    message=f"Pod '{resource}' cannot be scheduled. {message}",
                    reason="FailedScheduling",
                    raw_context={
                        "scheduler_reason": scheduler_reason,
                        "event_message": message,
                        "pod_phase": phase
                    }
                ))
                
                # Only report first scheduling failure
                break
        
        return findings
    
    def _parse_scheduler_reason(self, message: str) -> str:
        """
        Extract structured reason from scheduler event message.
        
        Example messages:
        - "0/3 nodes are available: 3 Insufficient cpu."
        - "0/3 nodes are available: 3 Insufficient memory."
        - "0/3 nodes are available: 3 node(s) didn't match pod affinity rules."
        - "0/3 nodes are available: 3 node(s) had taints that the pod didn't tolerate."
        """
        message_lower = message.lower()
        
        if "insufficient cpu" in message_lower:
            return "InsufficientCPU"
        elif "insufficient memory" in message_lower:
            return "InsufficientMemory"
        elif "insufficient pods" in message_lower:
            return "InsufficientPods"
        elif "no nodes available" in message_lower:
            return "NoNodesAvailable"
        elif "didn't match pod affinity" in message_lower or "didn't match node selector" in message_lower:
            return "NodeAffinityMismatch"
        elif "didn't tolerate taint" in message_lower or "had taints" in message_lower:
            return "TaintNotTolerated"
        elif "unschedulable" in message_lower:
            return "Unschedulable"
        else:
            return "UnknownSchedulingFailure"
