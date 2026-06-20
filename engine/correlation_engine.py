from __future__ import annotations
from datetime import datetime, timedelta
from collections import defaultdict

from engine.incident_store import IncidentStore
from engine.finding_store import FindingStore
from engine.incident_models import IncidentStatus, FindingRole, Incident


class CorrelationEngine:
    """
    Groups findings into incidents using five prioritised rules:
      Rule 0: Pod-inferred controller  — ≥2 pod findings sharing the same owner
                                         imply a controller (Deployment/StatefulSet)
                                         level incident
      Rule 1: Owner dominance          — explicit parent-resource finding
                                         suppresses its child pod findings
      Rule 2: Node dominance           — node failure suppresses all pod failures
                                         on that node
      Rule 3: Namespace aggregation    — pre-aggregated namespace-level finding
                                         becomes a namespace incident
      Rule 4: Standalone escalation    — definitive single-resource failures
                                         (NoEndpoints, ZeroAvailableReplicas, …)
                                         that were not covered by rules 0-3 get
                                         their own incident, with cross-resource
                                         deduplication to avoid duplicates when
                                         e.g. Service and Deployment for the same
                                         app both fail simultaneously.
    """
    
    def __init__(self, incident_store: IncidentStore, finding_store: FindingStore | None = None):
        self.store = incident_store
        self.finding_store = finding_store
    
    def correlate(self, findings: list[dict]) -> list[dict]:
        """Run all correlation rules and return findings suppressed as symptoms."""
        if not findings:
            return []
        
        suppressed = []
        
        # Rule 0: infer controller failure from pod-only findings (highest priority)
        suppressed.extend(self._infer_controller_incidents(findings))
        # Rule 1: parent resource failure dominates children
        suppressed.extend(self._apply_owner_dominance(findings))
        # Rule 2: node failure dominates pods on that node
        suppressed.extend(self._apply_node_dominance(findings))
        # Rule 3: namespace-level aggregated findings
        suppressed.extend(self._handle_namespace_aggregations(findings))
        # Rule 4: escalate any CRITICAL finding still without an incident
        self._escalate_standalone_criticals(findings, suppressed)
        
        return suppressed
    
    def _infer_controller_incidents(self, findings: list[dict]) -> list[dict]:
        """Rule 0: infer a controller incident from >=2 pod findings sharing the same owner."""
        suppressed = []
        
        # Extract pod findings from current batch
        pod_findings = [f for f in findings if f.get("scope") == "Pod"]
        if not pod_findings:
            return []

        processed_controllers = set()
        
        for pod in pod_findings:
            owner_chain_str = pod.get("raw_context", {}).get("owner_chain", "")
            if not owner_chain_str:
                continue

            owners = owner_chain_str.split(",")
            if not owners:
                continue

            top_owner = owners[-1].strip()
            parts = top_owner.split("/")
            
            if len(parts) != 3:
                continue

            controller_kind, controller_ns, controller_name = parts

            if controller_kind not in ["Deployment", "StatefulSet", "DaemonSet"]:
                continue

            controller_key = (controller_kind, controller_ns, controller_name)

            if controller_key in processed_controllers:
                continue
            processed_controllers.add(controller_key)
            
            all_controller_pods = self._get_controller_pod_findings(
                controller_kind, controller_ns, controller_name
            )

            if len(all_controller_pods) < 2:
                continue

            reasons = [p["reason"] for p in all_controller_pods]
            primary_reason = max(set(reasons), key=reasons.count)

            severities = [p["severity"] for p in all_controller_pods]
            primary_severity = "CRITICAL" if "CRITICAL" in severities else "WARNING"
            
            controller_resource = f"{controller_ns}/{controller_name}"
            controller_uid = f"{controller_kind}/{controller_resource}"

            existing = self.store.find_incident_by_resource(
                scope=controller_kind,
                resource_uid=controller_uid,
                status=IncidentStatus.ACTIVE
            )
            
            if not existing:
                # Create controller-scoped incident
                incident = self.store.create_incident(
                    title=f"{controller_kind} {controller_resource} failing",
                    scope=controller_kind,
                    resource=controller_resource,
                    resource_uid=controller_uid,
                    severity=primary_severity,
                    root_cause=primary_reason,
                    correlation_rule="pod_inferred_controller",
                    correlation_reason=f"Inferred from {len(all_controller_pods)} pods in {primary_reason}"
                )
                
                print(f"\n[INCIDENT CREATED] {incident.id}")
                print(f"   Title: {incident.title}")
                print(f"   Rule: pod_inferred_controller")
                print(f"   Reason: {incident.correlation_reason}")
                print(f"   Suppressing {len(all_controller_pods)} pod symptoms\n")
            else:
                incident = existing
            
            # Link ALL controller pods as symptoms (from database, not just current batch)
            for pod_finding in all_controller_pods:
                if pod_finding.get("id"):
                    self.store.link_finding_to_incident(
                        incident.id,
                        pod_finding["id"],
                        FindingRole.SYMPTOM
                    )

            for pod in pod_findings:
                pod_owner_chain = pod.get("raw_context", {}).get("owner_chain", "")
                if f"{controller_kind}/{controller_ns}/{controller_name}" in pod_owner_chain:
                    suppressed.append(pod)
        
        return suppressed
    
    def _get_controller_pod_findings(self, controller_kind: str, controller_ns: str, controller_name: str) -> list[dict]:
        """Query all pod findings for a controller within the last 2 hours."""
        if self.finding_store is None:
            return []
        controller_pattern = f"%{controller_kind}/{controller_ns}/{controller_name}%"
        cutoff = (datetime.utcnow() - timedelta(hours=2)).isoformat()
        return self.finding_store.find_by_owner_chain(controller_pattern, "Pod", cutoff)
    
    def _apply_owner_dominance(self, findings: list[dict]) -> list[dict]:
        """Rule 1: suppress pod findings when a parent Deployment/StatefulSet/DaemonSet is also failing."""
        suppressed = []
        
        parent_kinds = ["Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"]
        parents = [f for f in findings if f.get("scope") in parent_kinds]
        pods = [f for f in findings if f.get("scope") == "Pod"]
        
        if not parents or not pods:
            return []
        
        for parent in parents:
            parent_namespace = parent["resource"].split("/")[0] if "/" in parent["resource"] else "default"
            parent_name = parent["resource"].split("/")[1] if "/" in parent["resource"] else parent["resource"]
            parent_kind = parent["scope"]

            parent_identifier = f"{parent_kind}/{parent_namespace}/{parent_name}"
            owned_pods = []
            for pod in pods:
                owner_chain_str = pod.get("raw_context", {}).get("owner_chain", "")
                if parent_identifier in owner_chain_str:
                    owned_pods.append(pod)
            
            if not owned_pods:
                continue

            resource_uid = parent.get("resource_uid")
            existing = self.store.find_incident_by_resource(
                scope=parent["scope"],
                resource_uid=resource_uid,
                status=IncidentStatus.ACTIVE
            )
            
            if not existing:
                incident = self.store.create_incident(
                    title=f"{parent['scope']} {parent['resource']} failing",
                    scope=parent["scope"],
                    resource=parent["resource"],
                    resource_uid=resource_uid,
                    severity=parent["severity"],
                    root_cause=parent["reason"],
                    correlation_rule="owner_dominance",
                    correlation_reason=f"Parent {parent['scope']} failure dominates {len(owned_pods)} pod failures"
                )

                if parent.get("id"):
                    self.store.link_finding_to_incident(incident.id, parent["id"], FindingRole.ROOT_CAUSE)
            else:
                incident = existing

            for pod in owned_pods:
                if pod.get("id"):
                    self.store.link_finding_to_incident(incident.id, pod["id"], FindingRole.SYMPTOM)
                    suppressed.append(pod)
        
        return suppressed
    
    def _apply_node_dominance(self, findings: list[dict]) -> list[dict]:
        """Rule 2: suppress pod findings when their host node is unhealthy."""
        suppressed = []
        
        node_failures = [f for f in findings if f.get("scope") == "Node" and f.get("reason") in [
            "NodeNotReady", "NodeDiskPressure", "NodeMemoryPressure", "NodePIDPressure"
        ]]

        if not node_failures:
            return []

        pod_by_node = defaultdict(list)
        for finding in findings:
            if finding.get("scope") == "Pod":
                node_name = finding.get("raw_context", {}).get("node_name")
                if node_name:
                    pod_by_node[node_name].append(finding)

        for node_finding in node_failures:
            node_name = node_finding["resource"]
            affected_pods = pod_by_node.get(node_name, [])

            if not affected_pods:
                continue

            resource_uid = node_finding.get("resource_uid")
            existing = self.store.find_incident_by_resource(
                scope="Node",
                resource_uid=resource_uid,
                status=IncidentStatus.ACTIVE
            )

            if not existing:
                incident = self.store.create_incident(
                    title=f"Node {node_name} unhealthy",
                    scope="Node",
                    resource=node_name,
                    resource_uid=resource_uid,
                    severity="CRITICAL",
                    root_cause=node_finding["reason"],
                    correlation_rule="node_dominance",
                    correlation_reason=f"Node failure affects {len(affected_pods)} pods"
                )
                if node_finding.get("id"):
                    self.store.link_finding_to_incident(incident.id, node_finding["id"], FindingRole.ROOT_CAUSE)
            else:
                incident = existing

            for pod in affected_pods:
                if pod.get("id"):
                    self.store.link_finding_to_incident(incident.id, pod["id"], FindingRole.SYMPTOM)
                    suppressed.append(pod)
        
        return suppressed
    
    def _handle_namespace_aggregations(self, findings: list[dict]) -> list[dict]:
        """Rule 3: create incidents for pre-aggregated namespace findings (no suppression)."""
        namespace_findings = [f for f in findings if f.get("scope") == "Namespace"]
        
        for finding in namespace_findings:
            resource = finding['resource']
            resource_uid = finding.get("resource_uid", "")
            
            existing = self.store.find_incident_by_resource(
                scope="Namespace",
                resource_uid=resource_uid,
                status=IncidentStatus.ACTIVE
            )
            
            if not existing:
                category = finding.get("raw_context", {}).get("category", "Unknown")
                count = finding.get("raw_context", {}).get("finding_count", 0)

                incident = self.store.create_incident(
                    title=f"Namespace {resource}: {count} {category} failures",
                    scope="Namespace",
                    resource=resource,
                    resource_uid=resource_uid,
                    severity=finding["severity"],
                    root_cause=finding["reason"],
                    correlation_rule="namespace_aggregation",
                    correlation_reason=f"Multiple {category} failures in namespace"
                )

                if finding.get("id"):
                    self.store.link_finding_to_incident(incident.id, finding["id"], FindingRole.RELATED)

        return []
    
    # ---------------------------------------------------------------------------
    # Rule 4 — Standalone escalation
    #
    # Only escalate reasons that unambiguously mean "broken right now".
    # Transient / overlapping reasons (Pending, UnavailableReplicas partial,
    # generic Error) are excluded — they're either noise or already covered
    # by rules 0-3.
    #
    # Key deduplication logic (in order):
    #   1. Finding already suppressed as a symptom by rules 0-3 → skip
    #   2. Exact-match: resource already has an ACTIVE incident → link as
    #      CONTRIBUTING (refresh coverage, no new incident)
    #   3. Cross-resource: a *related* resource's incident already covers
    #      this finding (e.g. Service/redis-cart when Deployment/redis-cart
    #      already has an incident) → link as CONTRIBUTING, no new incident
    #   4. None of the above → create a minimal, well-titled incident
    # ---------------------------------------------------------------------------

    # Reasons per scope that warrant a standalone incident.
    # Keep this list conservative — false positives are worse than missed alerts.
    _ESCALATABLE: dict[str, set] = {
        # A service with 0 endpoints means traffic is black-holing right now.
        "Service":     {"NoEndpoints"},
        # All replicas gone — deployment is completely down.
        "Deployment":  {"ZeroAvailableReplicas"},
        # All pods gone in a StatefulSet.
        "StatefulSet": {"ZeroReadyReplicas"},
        # Single-pod deployments / ownerless pods that are permanently broken.
        # Only CrashLoop (persistent) and OOMKilled (memory bug); not Pending
        # or ErrImagePull — those are already handled by Rule 0 when ≥2 pods.
        "Pod":         {"CrashLoopBackOff", "OOMKilled", "MemoryPressure"},
        # Node-level: Rule 2 handles node+pods together, but if only the node
        # finding arrives (no pods yet), we still want an incident.
        "Node":        {"NodeNotReady"},
    }

    def _escalate_standalone_criticals(self, findings: list[dict], suppressed: list[dict]) -> None:
        """
        Rule 4: Escalate findings that are definitively broken but not covered by
        rules 0-3. Applies cross-resource deduplication before creating any incident
        so that related failures (e.g. Service + Deployment for the same app) are
        grouped, not duplicated.
        """
        suppressed_ids = {f.get("id") for f in suppressed if f.get("id")}

        for finding in findings:
            scope  = finding.get("scope", "")
            reason = finding.get("reason", "")

            # Only escalate whitelisted (scope, reason) pairs.
            if reason not in self._ESCALATABLE.get(scope, set()):
                continue

            # Already suppressed as a symptom of a higher-order incident.
            if finding.get("id") in suppressed_ids:
                continue

            resource     = finding.get("resource", "unknown")
            resource_uid = finding.get("resource_uid") or ""
            severity     = finding.get("severity", "CRITICAL")

            # Stable lookup key — use the K8s uid when available, otherwise
            # fall back to a deterministic "Scope/namespace/name" string so
            # the same resource never spawns two concurrent incidents.
            lookup_uid = (
                resource_uid
                if resource_uid and resource_uid not in ("", "unknown")
                else f"{scope}/{resource}"
            )

            # ── 1. Exact-match dedup ────────────────────────────────────────
            existing = self.store.find_incident_by_resource(
                scope=scope,
                resource_uid=lookup_uid,
                status=IncidentStatus.ACTIVE,
            )
            if existing:
                if finding.get("id"):
                    self.store.link_finding_to_incident(
                        existing.id, finding["id"], FindingRole.CONTRIBUTING
                    )
                continue

            # ── 2. Cross-resource dedup ─────────────────────────────────────
            # If a related resource already has an active incident that logically
            # covers this finding, link in rather than duplicating.
            related = self._find_related_incident(scope, resource)
            if related:
                if finding.get("id"):
                    self.store.link_finding_to_incident(
                        related.id, finding["id"], FindingRole.CONTRIBUTING
                    )
                ns, name = (resource.split("/", 1) + [""])[:2]
                print(f"[Rule 4] {scope}/{resource} ({reason}) → linked to "
                      f"existing incident '{related.title}' ({related.id[:8]}…)")
                continue

            # ── 3. Create a new standalone incident ─────────────────────────
            title  = self._incident_title(scope, resource, reason)
            ns, name = (resource.split("/", 1) + [""])[:2]
            incident = self.store.create_incident(
                title=title,
                scope=scope,
                resource=resource,
                resource_uid=lookup_uid,
                severity=severity,
                root_cause=reason,
                correlation_rule="standalone_escalation",
                correlation_reason=(
                    f"{scope} '{name}' in namespace '{ns}' "
                    f"has a definitive failure ({reason}) with no "
                    "higher-order correlation match."
                ),
            )

            print(f"\n[INCIDENT CREATED] {incident.id}")
            print(f"   Title: {incident.title}")
            print(f"   Rule:  standalone_escalation ({scope}/{reason})")
            print(f"   Resource: {resource}\n")

            if finding.get("id"):
                self.store.link_finding_to_incident(
                    incident.id, finding["id"], FindingRole.ROOT_CAUSE
                )

    def _find_related_incident(self, scope: str, resource: str) -> Incident | None:
        """
        Look for an already-active incident that logically covers this finding,
        to avoid creating duplicate incidents for the same root cause.

        Relationships checked:
          Service     → Deployment or StatefulSet with the same name in the same namespace
          Pod (no owner that reached Rule 0) → nothing extra to check here
        """
        if "/" not in resource:
            return None
        namespace, name = resource.split("/", 1)

        if scope == "Service":
            # A service losing endpoints is often caused by its backing
            # Deployment going down — check both controller types.
            for parent_scope in ("Deployment", "StatefulSet"):
                parent_uid = f"{parent_scope}/{namespace}/{name}"
                inc = self.store.find_incident_by_resource(
                    scope=parent_scope,
                    resource_uid=parent_uid,
                    status=IncidentStatus.ACTIVE,
                )
                if inc:
                    return inc

        return None

    @staticmethod
    def _incident_title(scope: str, resource: str, reason: str) -> str:
        """Human-readable incident title derived from scope + reason."""
        titles = {
            ("Service",     "NoEndpoints"):           f"Service {resource} has no endpoints",
            ("Deployment",  "ZeroAvailableReplicas"): f"Deployment {resource} — all replicas unavailable",
            ("StatefulSet", "ZeroReadyReplicas"):     f"StatefulSet {resource} — all pods down",
            ("Pod",         "CrashLoopBackOff"):      f"Pod {resource} is crash-looping",
            ("Pod",         "OOMKilled"):             f"Pod {resource} OOM-killed",
            ("Pod",         "MemoryPressure"):        f"Pod {resource} — sustained memory pressure",
            ("Node",        "NodeNotReady"):          f"Node {resource} is not ready",
        }
        return titles.get((scope, reason), f"{scope} {resource} — {reason}")

    def auto_resolve_stale_incidents(self, stale_threshold: timedelta = timedelta(minutes=15)) -> int:
        """Resolve ACTIVE incidents with no new findings for stale_threshold; return count resolved."""
        active_incidents = self.store.list_active_incidents()
        now = datetime.utcnow()
        resolved_count = 0
        
        for incident in active_incidents:
            time_since_update = now - incident.last_seen
            if time_since_update > stale_threshold:
                self.store.resolve_incident(incident.id)
                resolved_count += 1
        
        return resolved_count
