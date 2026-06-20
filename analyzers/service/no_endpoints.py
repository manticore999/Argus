"""
Service Endpoint Missing Analyzer

Detects services with no endpoints (no pods backing the service).

K8s Behavior:
- Service exists but selector matches zero pods
- Or matched pods exist but none are ready
- Results in connection failures for clients using the service

Common causes:
- Deployment scaled to 0
- All pods failing (CrashLoop, Pending)
- Label selector mismatch
- No pods in Ready state
"""

from analyzers.base import Analyzer, Finding, Severity


class ServiceEndpointMissingAnalyzer(Analyzer):
    """
    Detects services with no endpoints.
    
    Note: This analyzer expects the snapshot to include endpoint information.
    The fetcher should include this via a separate API call or embedded in snapshot.
    """

    @property
    def name(self) -> str:
        return "ServiceEndpointMissingAnalyzer"

    @property
    def supported_kinds(self) -> list[str]:
        return ["Service"]

    def analyze(self, snapshot: dict) -> list[Finding]:
        findings = []

        metadata = snapshot.get("metadata") or {}
        spec = snapshot.get("spec") or {}

        service_name = metadata.get("name", "unknown")
        namespace = metadata.get("namespace", "default")
        resource_id = f"{namespace}/{service_name}"

        # Skip services without selectors (ExternalName, headless without selector)
        selector = spec.get("selector")
        if not selector:
            return findings

        # Check endpoint information (must be provided in snapshot)
        # Fetcher should attach endpoint_addresses count
        endpoint_count = snapshot.get("endpoint_addresses", -1)

        if endpoint_count == 0:
            # Service has selector but no endpoints
            # Enhanced context for correlation engine (Rule 4)
            ports = spec.get("ports", [])
            target_port = ports[0].get("targetPort") if ports else None
            service_type = spec.get("type", "ClusterIP")
            
            findings.append(Finding(
                analyzer=self.name,
                resource=resource_id,
                kind="Service",
                severity=Severity.CRITICAL,
                reason="NoEndpoints",
                message=f"Service '{service_name}' in namespace '{namespace}' has no endpoints. "
                        f"No pods match the selector or all matching pods are not ready.",
                raw_context={
                    "service": service_name,
                    "namespace": namespace,
                    "selector": selector,  # For linking to pods
                    "target_port": target_port,
                    "service_type": service_type,
                    "expected_pods": snapshot.get("expected_pods", 0)  # From fetcher
                }
            ))
        elif endpoint_count == -1:
            # Endpoint count not provided in snapshot (fetcher limitation)
            # Emit a warning that we can't analyze this properly
            findings.append(Finding(
                analyzer=self.name,
                resource=resource_id,
                kind="Service",
                severity=Severity.INFO,
                reason="EndpointCheckSkipped",
                message=f"Service '{service_name}' endpoint check skipped (endpoint data not in snapshot)",
                raw_context={
                    "service": service_name,
                    "namespace": namespace,
                    "note": "Fetcher needs to include endpoint information"
                }
            ))

        return findings
