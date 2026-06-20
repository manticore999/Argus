from collections import defaultdict
from datetime import datetime

from analyzers.base import Finding


class NamespaceAggregator:
    """
    Post-processor that groups findings by (namespace, failure_category).
    Detects namespace-wide issues: registry outages, bad deployment waves, cluster exhaustion.
    Runs after individual analyzers and emits Namespace-scoped findings.
    """
    
    def __init__(self, window_seconds: int = 60, min_threshold: int = 3):
        self.window_seconds = window_seconds
        self.min_threshold = min_threshold
    
    def aggregate(self, findings: list[Finding]) -> list[Finding]:
        """Return namespace-level findings if a pattern threshold is met, otherwise empty list."""
        if len(findings) < self.min_threshold:
            return []

        groups = defaultdict(list)
        
        for finding in findings:
            resource_parts = finding.resource.split('/', 1)
            if len(resource_parts) != 2:
                continue

            namespace = resource_parts[0]
            category = self._categorize(finding.reason)
            groups[(namespace, category)].append(finding)

        aggregated = []

        for (namespace, category), group_findings in groups.items():
            if len(group_findings) < self.min_threshold:
                continue

            if not self._within_time_window(group_findings):
                continue
            
            # Create aggregated finding
            affected_resources = [f.resource for f in group_findings]
            severities = [f.severity for f in group_findings]
            max_severity = max(severities, key=lambda s: s.value)
            
            aggregated_finding = Finding(
                analyzer="NamespaceAggregator",
                resource=namespace,  # Namespace-level resource
                kind="Namespace",
                severity=max_severity,
                message=f"Namespace '{namespace}' has {len(group_findings)} {category} failures across multiple resources",
                reason=f"Namespace{category}",
                # Stable uid: dedup key = (namespace::category, NamespaceAggregator, reason)
                # Must NOT be empty — an empty uid causes all namespace findings
                # across all namespaces to share the same DB row.
                resource_uid=f"{namespace}::{category}",
                owner_chain=[],
                timestamp=group_findings[0].timestamp,
                raw_context={
                    "affected_resources": affected_resources,
                    "failure_category": category,
                    "count": len(group_findings),
                    "time_window_seconds": self.window_seconds,
                    "grouped_findings": [
                        {
                            "resource": f.resource,
                            "reason": f.reason,
                            "message": f.message
                        }
                        for f in group_findings
                    ]
                }
            )
            
            aggregated.append(aggregated_finding)
        
        return aggregated
    
    def _categorize(self, reason: str) -> str:
        """Map a finding reason to a broad failure category for grouping."""
        reason_lower = reason.lower()
        
        if "imagepull" in reason_lower or "errimagepull" in reason_lower:
            return "ImagePull"
        elif "crashloop" in reason_lower:
            return "CrashLoop"
        elif "scheduling" in reason_lower or "insufficient" in reason_lower:
            return "Scheduling"
        elif "failedmount" in reason_lower or "volume" in reason_lower or "pvc" in reason_lower:
            return "Storage"
        elif "notready" in reason_lower:
            return "NotReady"
        else:
            return "Other"
    
    def _within_time_window(self, findings: list[Finding]) -> bool:
        """Return True if the findings span is within the configured window."""
        if not findings:
            return False

        timestamps = []
        for f in findings:
            if not f.timestamp:
                continue
            try:
                timestamps.append(datetime.fromisoformat(f.timestamp))
            except (ValueError, AttributeError):
                continue

        if len(timestamps) < 2:
            return True

        time_span = (max(timestamps) - min(timestamps)).total_seconds()
        return time_span <= self.window_seconds
