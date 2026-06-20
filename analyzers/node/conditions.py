"""
Node Condition Analyzer - Detects node pressure and health issues.
Enables: Node → Pod dominance correlation (Rule 2).
"""

from typing import Any
from analyzers.base import Analyzer, Finding, Severity


class NodeConditionAnalyzer(Analyzer):
    """
    Detects node conditions that cause pod failures:
    - NotReady (node offline/kubelet issues)
    - DiskPressure (disk full → pod evictions)
    - MemoryPressure (OOM → pod evictions)
    - PIDPressure (process limit → pod failures)
    - NetworkUnavailable (network issues)
    
    This is a DOMINANT analyzer - enables correlation engine to suppress
    pod failures on unhealthy nodes.
    """

    @property
    def name(self) -> str:
        return "NodeConditionAnalyzer"

    @property
    def supported_kinds(self) -> list[str]:
        return ["Node"]

    def analyze(self, snapshot: dict[str, Any]) -> list[Finding]:
        findings: list[Finding] = []
        meta = snapshot.get("metadata", {})
        node_name = meta.get("name", "unknown")
        
        conditions = snapshot.get("status", {}).get("conditions", [])
        
        for condition in conditions:
            condition_type = condition.get("type")
            status = condition.get("status")
            reason = condition.get("reason", "")
            message = condition.get("message", "")
            
            # Ready condition: status should be "True" for healthy node
            if condition_type == "Ready":
                if status != "True":
                    findings.append(Finding(
                        analyzer=self.name,
                        resource=node_name,
                        kind="Node",
                        severity=Severity.CRITICAL,
                        message=f"Node '{node_name}' is NotReady. Reason: {reason}. {message}",
                        reason="NotReady",
                        raw_context={
                            "condition_type": condition_type,
                            "condition_status": status,
                            "condition_reason": reason,
                            "condition_message": message
                        }
                    ))
            
            # Pressure conditions: status should be "False" for healthy node
            elif condition_type in ["DiskPressure", "MemoryPressure", "PIDPressure", "NetworkUnavailable"]:
                if status == "True":
                    # Determine severity
                    severity = Severity.CRITICAL if condition_type in ["DiskPressure", "MemoryPressure"] else Severity.WARNING
                    
                    findings.append(Finding(
                        analyzer=self.name,
                        resource=node_name,
                        kind="Node",
                        severity=severity,
                        message=f"Node '{node_name}' has {condition_type}. {message}",
                        reason=condition_type,
                        raw_context={
                            "condition_type": condition_type,
                            "condition_status": status,
                            "condition_reason": reason,
                            "condition_message": message
                        }
                    ))
        
        return findings
