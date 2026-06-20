"""Tool registry for the AIOps chat agent — declarations + executors."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from google.genai import types as genai_types
from kubernetes.client import CoreV1Api, AppsV1Api
from kubernetes.client.exceptions import ApiException

from config import PROMETHEUS_URL
from k8s.client import load_kube_client
from engine.finding_store import FindingStore
from engine.incident_store import IncidentStore
from snapshot.fetcher import SnapshotFetcher


# ── Shared clients (lazy singletons) ─────────────────────────────────────────

_core: CoreV1Api | None = None
_apps: AppsV1Api | None = None
_fetcher: SnapshotFetcher | None = None
_finding_store: FindingStore | None = None
_incident_store: IncidentStore | None = None


def _k8s_core() -> CoreV1Api:
    global _core
    if _core is None:
        _core = load_kube_client()
    return _core


def _k8s_apps() -> AppsV1Api:
    global _apps
    if _apps is None:
        _apps = AppsV1Api(_k8s_core().api_client)
    return _apps


def _snapshot_fetcher() -> SnapshotFetcher:
    global _fetcher
    if _fetcher is None:
        _fetcher = SnapshotFetcher(_k8s_core())
    return _fetcher


def _findings() -> FindingStore:
    global _finding_store
    if _finding_store is None:
        _finding_store = FindingStore()
    return _finding_store


def _incidents() -> IncidentStore:
    global _incident_store
    if _incident_store is None:
        _incident_store = IncidentStore()
    return _incident_store


# ═════════════════════════════════════════════════════════════════════════════
# Tool implementations
# ═════════════════════════════════════════════════════════════════════════════

def get_active_incidents(**kwargs) -> str:
    """Return all active incidents from the DB."""
    rows = _incidents().list_active_incidents()
    if not rows:
        return "No active incidents."
    results = []
    for inc in rows:
        diag = _incidents().get_diagnosis(inc.id)
        results.append({
            "id": inc.id,
            "title": inc.title,
            "severity": inc.severity,
            "scope": inc.scope,
            "resource": inc.resource,
            "correlation_rule": inc.correlation_rule,
            "finding_count": inc.finding_count,
            "first_seen": inc.first_seen.isoformat(),
            "last_seen": inc.last_seen.isoformat(),
            "has_diagnosis": diag is not None,
        })
    return json.dumps(results, indent=2)


def get_incident_detail(incident_id: str, **kwargs) -> str:
    """Return full detail for one incident including linked findings and diagnosis."""
    inc = _incidents().get_incident_by_id(incident_id)
    if not inc:
        return f"Incident '{incident_id}' not found."
    diag = _incidents().get_diagnosis(incident_id)

    links = _incidents().get_linked_findings(incident_id)

    result = {
        "incident": {
            "id": inc.id, "title": inc.title, "severity": inc.severity,
            "scope": inc.scope, "resource": inc.resource,
            "status": inc.status.value if hasattr(inc.status, 'value') else str(inc.status),
            "root_cause": inc.root_cause,
            "correlation_rule": inc.correlation_rule,
            "correlation_reason": inc.correlation_reason,
            "finding_count": inc.finding_count,
            "first_seen": inc.first_seen.isoformat() if hasattr(inc.first_seen, 'isoformat') else str(inc.first_seen),
            "last_seen": inc.last_seen.isoformat() if hasattr(inc.last_seen, 'isoformat') else str(inc.last_seen),
        },
        "findings": [
            {
                "id": r["id"], "kind": r["kind"], "resource": r["resource"],
                "reason": r["reason"], "severity": r["severity"],
                "message": r["message"], "role": r["role"],
                "log_snippet": (r.get("log_snippet") or "")[:500],
            }
            for r in links
        ],
        "diagnosis": diag,
    }
    return json.dumps(result, indent=2)


def get_recent_findings(namespace: str = "", limit: int = 20, **kwargs) -> str:
    """Return recent findings, optionally filtered by namespace."""
    rows = _findings().get_recent(limit=limit)
    if namespace:
        rows = [r for r in rows if namespace in r.get("resource", "")]
    if not rows:
        return f"No recent findings{' in ' + namespace if namespace else ''}."
    # Trim large fields
    for r in rows:
        r.pop("log_snippet", None)
    return json.dumps(rows, indent=2)


def list_pods(namespace: str, **kwargs) -> str:
    """List all pods in a namespace with status summary."""
    try:
        pods = _k8s_core().list_namespaced_pod(namespace=namespace)
    except ApiException as e:
        return f"Error listing pods in '{namespace}': {e.reason}"
    results = []
    for p in pods.items:
        phase = p.status.phase
        ready = "0/0"
        restarts = 0
        if p.status.container_statuses:
            total = len(p.status.container_statuses)
            rdy = sum(1 for c in p.status.container_statuses if c.ready)
            ready = f"{rdy}/{total}"
            restarts = sum(c.restart_count for c in p.status.container_statuses)
        results.append({
            "name": p.metadata.name,
            "phase": phase,
            "ready": ready,
            "restarts": restarts,
            "node": p.spec.node_name,
            "age": _age(p.metadata.creation_timestamp),
        })
    return json.dumps(results, indent=2)


def list_deployments(namespace: str, **kwargs) -> str:
    """List all deployments in a namespace with replica counts."""
    try:
        deps = _k8s_apps().list_namespaced_deployment(namespace=namespace)
    except ApiException as e:
        return f"Error listing deployments in '{namespace}': {e.reason}"
    results = []
    for d in deps.items:
        results.append({
            "name": d.metadata.name,
            "replicas": d.spec.replicas,
            "ready": d.status.ready_replicas or 0,
            "available": d.status.available_replicas or 0,
            "unavailable": d.status.unavailable_replicas or 0,
            "age": _age(d.metadata.creation_timestamp),
        })
    return json.dumps(results, indent=2)


def describe_resource(kind: str, name: str, namespace: str = "", **kwargs) -> str:
    """Fetch a detailed snapshot of a single K8s resource."""
    fetcher = _snapshot_fetcher()
    kind_lower = kind.lower()
    try:
        if kind_lower == "pod":
            snap = fetcher.fetch_pod_snapshot(namespace, name)
        elif kind_lower == "deployment":
            snap = fetcher.fetch_deployment_snapshot(namespace, name)
        elif kind_lower == "service":
            snap = fetcher.fetch_service_snapshot(namespace, name)
        elif kind_lower == "node":
            snap = fetcher.fetch_node_snapshot(name)
        elif kind_lower == "statefulset":
            snap = fetcher.fetch_statefulset_snapshot(namespace, name)
        elif kind_lower == "daemonset":
            snap = fetcher.fetch_daemonset_snapshot(namespace, name)
        elif kind_lower == "job":
            snap = fetcher.fetch_job_snapshot(namespace, name)
        elif kind_lower == "pvc":
            snap = fetcher.fetch_pvc_snapshot(namespace, name)
        else:
            return f"Unsupported kind: '{kind}'. Supported: Pod, Deployment, Service, Node, StatefulSet, DaemonSet, Job, PVC."
    except ApiException as e:
        return f"Error describing {kind}/{namespace}/{name}: {e.reason}"

    if not snap:
        return f"{kind} '{name}' not found in namespace '{namespace}'."
    return json.dumps(snap, indent=2, default=str)


def get_pod_logs(name: str, namespace: str, container: str = "", tail_lines: int = 80, **kwargs) -> str:
    """Fetch recent logs from a pod container."""
    try:
        logs = _k8s_core().read_namespaced_pod_log(
            name=name,
            namespace=namespace,
            container=container or None,
            tail_lines=tail_lines,
            timestamps=True,
        )
        return logs if logs else "(empty log output)"
    except ApiException as e:
        return f"Error fetching logs for {namespace}/{name}: {e.reason}"


def list_events(namespace: str, resource_name: str = "", **kwargs) -> str:
    """List recent K8s events in a namespace, optionally filtered to a resource."""
    try:
        if resource_name:
            events = _k8s_core().list_namespaced_event(
                namespace=namespace,
                field_selector=f"involvedObject.name={resource_name}",
            )
        else:
            events = _k8s_core().list_namespaced_event(namespace=namespace)
    except ApiException as e:
        return f"Error listing events in '{namespace}': {e.reason}"

    items = sorted(events.items, key=lambda e: e.last_timestamp or e.event_time or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    results = []
    for ev in items[:30]:  # cap output
        results.append({
            "type": ev.type,
            "reason": ev.reason,
            "object": f"{ev.involved_object.kind}/{ev.involved_object.name}",
            "message": ev.message,
            "count": ev.count,
            "last_seen": str(ev.last_timestamp or ev.event_time or ""),
        })
    return json.dumps(results, indent=2)


def list_nodes(**kwargs) -> str:
    """List all cluster nodes with conditions and resource capacity."""
    try:
        nodes = _k8s_core().list_node()
    except ApiException as e:
        return f"Error listing nodes: {e.reason}"
    results = []
    for n in nodes.items:
        conditions = {}
        for c in (n.status.conditions or []):
            conditions[c.type] = c.status
        results.append({
            "name": n.metadata.name,
            "conditions": conditions,
            "cpu_capacity": n.status.capacity.get("cpu", "?"),
            "memory_capacity": n.status.capacity.get("memory", "?"),
            "pods_capacity": n.status.capacity.get("pods", "?"),
            "kubelet_version": n.status.node_info.kubelet_version if n.status.node_info else "?",
        })
    return json.dumps(results, indent=2)


def list_namespaces(**kwargs) -> str:
    """List all namespaces in the cluster."""
    try:
        nss = _k8s_core().list_namespace()
    except ApiException as e:
        return f"Error listing namespaces: {e.reason}"
    results = [
        {"name": ns.metadata.name, "status": ns.status.phase}
        for ns in nss.items
    ]
    return json.dumps(results, indent=2)


def query_prometheus(promql: str, **kwargs) -> str:
    """Execute an arbitrary PromQL query against the Prometheus server."""
    try:
        from prometheus_api_client import PrometheusConnect
        prom = PrometheusConnect(url=PROMETHEUS_URL, disable_ssl=True)
        results = prom.custom_query(promql)
        if not results:
            return f"Query returned 0 results: {promql}"
        # Trim to 20 results max
        trimmed = results[:20]
        output = []
        for r in trimmed:
            output.append({
                "metric": r.get("metric", {}),
                "value": r.get("value", []),
            })
        summary = f"{len(results)} result(s)"
        if len(results) > 20:
            summary += f" (showing first 20)"
        return json.dumps({"summary": summary, "results": output}, indent=2)
    except ImportError:
        return "prometheus-api-client not installed."
    except Exception as e:
        return f"Prometheus query error: {e}"


def get_incident_history(resource: str = "", limit: int = 10, **kwargs) -> str:
    """Query past incidents (including resolved) for a resource pattern."""
    rows = _incidents().search_incidents(resource_pattern=resource, limit=limit)
    if not rows:
        return f"No incident history{' for ' + resource if resource else ''}."
    return json.dumps(rows, indent=2)


def get_cluster_health(**kwargs) -> str:
    """
    Return a single cluster-wide health snapshot:
    node readiness, pod counts per namespace, and active incident summary.
    Good starting point for any health question.
    """
    result: dict = {"nodes": [], "namespaces": [], "active_incidents": 0}

    # ── Nodes ──
    try:
        nodes = _k8s_core().list_node()
        for n in nodes.items:
            conditions = {c.type: c.status for c in (n.status.conditions or [])}
            result["nodes"].append({
                "name": n.metadata.name,
                "ready": conditions.get("Ready", "Unknown"),
                "memory_pressure": conditions.get("MemoryPressure", "Unknown"),
                "disk_pressure": conditions.get("DiskPressure", "Unknown"),
            })
    except ApiException as e:
        result["nodes_error"] = str(e.reason)

    # ── Pods per namespace ──
    try:
        all_pods = _k8s_core().list_pod_for_all_namespaces()
        ns_stats: dict[str, dict] = {}
        for p in all_pods.items:
            ns = p.metadata.namespace
            if ns not in ns_stats:
                ns_stats[ns] = {"total": 0, "running": 0, "not_running": 0, "restarts": 0}
            ns_stats[ns]["total"] += 1
            phase = (p.status.phase or "").lower()
            if phase == "running":
                ns_stats[ns]["running"] += 1
            else:
                ns_stats[ns]["not_running"] += 1
            for cs in (p.status.container_statuses or []):
                ns_stats[ns]["restarts"] += cs.restart_count
        result["namespaces"] = [
            {"namespace": ns, **stats}
            for ns, stats in sorted(ns_stats.items())
        ]
    except ApiException as e:
        result["namespaces_error"] = str(e.reason)

    # ── Active incidents ──
    try:
        incidents = _incidents().list_active_incidents()
        result["active_incidents"] = len(incidents)
        result["incident_titles"] = [i.title for i in incidents[:5]]
    except Exception:
        result["active_incidents"] = -1

    return json.dumps(result, indent=2)


def list_services(namespace: str, **kwargs) -> str:
    """List all services in a namespace with their type, ports, and cluster IP."""
    try:
        svcs = _k8s_core().list_namespaced_service(namespace=namespace)
    except ApiException as e:
        return f"Error listing services in '{namespace}': {e.reason}"
    results = []
    for s in svcs.items:
        ports = []
        for p in (s.spec.ports or []):
            port_str = f"{p.port}"
            if p.target_port:
                port_str += f"→{p.target_port}"
            if p.protocol and p.protocol != "TCP":
                port_str += f"/{p.protocol}"
            if p.name:
                port_str = f"{p.name}:{port_str}"
            ports.append(port_str)
        results.append({
            "name": s.metadata.name,
            "type": s.spec.type,
            "cluster_ip": s.spec.cluster_ip,
            "ports": ports,
            "selector": s.spec.selector or {},
        })
    return json.dumps(results, indent=2)


def get_resource_pressure(namespace: str = "", **kwargs) -> str:
    """
    Return top pods by memory and CPU usage via Prometheus.
    Highlights anything over 80% of their configured limit.
    Works for any namespace; omit namespace for cluster-wide.
    """
    try:
        from prometheus_api_client import PrometheusConnect
        prom = PrometheusConnect(url=PROMETHEUS_URL, disable_ssl=True)
    except ImportError:
        return "prometheus-api-client not installed."
    except Exception as e:
        return f"Cannot connect to Prometheus: {e}"

    ns_filter = f'namespace="{namespace}"' if namespace else 'namespace!=""'
    results: dict = {"memory": [], "cpu": [], "over_limit": []}

    # Memory usage as % of limit
    try:
        mem_query = (
            f'sum by (pod, namespace) '
            f'(container_memory_working_set_bytes{{{ns_filter}, container!=""}})'
            f' / sum by (pod, namespace) '
            f'(kube_pod_container_resource_limits{{{ns_filter}, resource="memory", container!=""}})'
        )
        mem_rows = prom.custom_query(mem_query)
        mem_sorted = sorted(mem_rows, key=lambda r: float(r["value"][1]), reverse=True)[:10]
        for r in mem_sorted:
            pct = round(float(r["value"][1]) * 100, 1)
            entry = {
                "pod": r["metric"].get("pod", "?"),
                "namespace": r["metric"].get("namespace", "?"),
                "memory_pct_of_limit": pct,
            }
            results["memory"].append(entry)
            if pct >= 80:
                results["over_limit"].append({**entry, "resource": "memory"})
    except Exception as e:
        results["memory_error"] = str(e)

    # CPU usage as % of limit
    try:
        cpu_query = (
            f'sum by (pod, namespace) '
            f'(rate(container_cpu_usage_seconds_total{{{ns_filter}, container!=""}}[5m]))'
            f' / sum by (pod, namespace) '
            f'(kube_pod_container_resource_limits{{{ns_filter}, resource="cpu", container!=""}})'
        )
        cpu_rows = prom.custom_query(cpu_query)
        cpu_sorted = sorted(cpu_rows, key=lambda r: float(r["value"][1]), reverse=True)[:10]
        for r in cpu_sorted:
            pct = round(float(r["value"][1]) * 100, 1)
            entry = {
                "pod": r["metric"].get("pod", "?"),
                "namespace": r["metric"].get("namespace", "?"),
                "cpu_pct_of_limit": pct,
            }
            results["cpu"].append(entry)
            if pct >= 80:
                results["over_limit"].append({**entry, "resource": "cpu"})
    except Exception as e:
        results["cpu_error"] = str(e)

    if not results["memory"] and not results["cpu"]:
        return "No resource limit data available. Pods may not have resource limits configured."

    return json.dumps(results, indent=2)


# ── Utility ──────────────────────────────────────────────────────────────────

def _age(ts) -> str:
    """Human-readable age from a K8s timestamp."""
    if ts is None:
        return "unknown"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


# ═════════════════════════════════════════════════════════════════════════════
# Gemini FunctionDeclarations + registry
# ═════════════════════════════════════════════════════════════════════════════

_TOOL_DEFS: list[tuple[genai_types.FunctionDeclaration, Callable[..., str]]] = []


def _register(
    name: str,
    description: str,
    parameters: dict[str, Any],
    fn: Callable[..., str],
) -> None:
    """Register a tool with its Gemini declaration and callable."""
    decl = genai_types.FunctionDeclaration(
        name=name,
        description=description,
        parameters=parameters,
    )
    _TOOL_DEFS.append((decl, fn))


# ── DB tools ─────────────────────────────────────────────────────────────────

_register(
    name="get_active_incidents",
    description="List all currently active incidents from the AIOps database.",
    parameters={
        "type": "object",
        "properties": {},
    },
    fn=get_active_incidents,
)

_register(
    name="get_incident_detail",
    description=(
        "Get full detail for a specific incident: linked findings, roles, "
        "severity, correlation info, and LLM diagnosis if available."
    ),
    parameters={
        "type": "object",
        "properties": {
            "incident_id": {"type": "string", "description": "The UUID of the incident."},
        },
        "required": ["incident_id"],
    },
    fn=get_incident_detail,
)

_register(
    name="get_recent_findings",
    description="List recent findings (anomaly detections) from the DB, optionally filtered by namespace.",
    parameters={
        "type": "object",
        "properties": {
            "namespace": {"type": "string", "description": "Filter to this namespace (optional)."},
            "limit": {"type": "integer", "description": "Max results (default 20)."},
        },
    },
    fn=get_recent_findings,
)

_register(
    name="get_incident_history",
    description="Query past incidents (active + resolved) for a resource name pattern.",
    parameters={
        "type": "object",
        "properties": {
            "resource": {"type": "string", "description": "Substring match on resource name (optional)."},
            "limit": {"type": "integer", "description": "Max results (default 10)."},
        },
    },
    fn=get_incident_history,
)

# ── K8s tools ────────────────────────────────────────────────────────────────

_register(
    name="list_pods",
    description="List all pods in a Kubernetes namespace with their status, readiness, and restart count.",
    parameters={
        "type": "object",
        "properties": {
            "namespace": {"type": "string", "description": "The Kubernetes namespace."},
        },
        "required": ["namespace"],
    },
    fn=list_pods,
)

_register(
    name="list_deployments",
    description="List all deployments in a namespace with replica counts (desired, ready, unavailable).",
    parameters={
        "type": "object",
        "properties": {
            "namespace": {"type": "string", "description": "The Kubernetes namespace."},
        },
        "required": ["namespace"],
    },
    fn=list_deployments,
)

_register(
    name="describe_resource",
    description=(
        "Fetch a detailed snapshot of a single K8s resource. "
        "Supported kinds: Pod, Deployment, Service, Node, StatefulSet, DaemonSet, Job, PVC."
    ),
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "description": "Resource kind (e.g. Pod, Deployment, Service)."},
            "name": {"type": "string", "description": "Resource name."},
            "namespace": {"type": "string", "description": "Namespace (omit for cluster-scoped like Node)."},
        },
        "required": ["kind", "name"],
    },
    fn=describe_resource,
)

_register(
    name="get_pod_logs",
    description="Fetch recent log lines from a pod. Useful for diagnosing crashes and errors.",
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Pod name."},
            "namespace": {"type": "string", "description": "Pod namespace."},
            "container": {"type": "string", "description": "Container name (optional, defaults to first)."},
            "tail_lines": {"type": "integer", "description": "Number of recent lines (default 80)."},
        },
        "required": ["name", "namespace"],
    },
    fn=get_pod_logs,
)

_register(
    name="list_events",
    description=(
        "List recent Kubernetes events in a namespace. "
        "Events include warnings, scheduling failures, image pulls, etc. "
        "Optionally filter to a specific resource."
    ),
    parameters={
        "type": "object",
        "properties": {
            "namespace": {"type": "string", "description": "The namespace to query events from."},
            "resource_name": {"type": "string", "description": "Filter events to this resource name (optional)."},
        },
        "required": ["namespace"],
    },
    fn=list_events,
)

_register(
    name="list_nodes",
    description="List all cluster nodes with conditions (Ready, MemoryPressure, etc.) and capacity.",
    parameters={
        "type": "object",
        "properties": {},
    },
    fn=list_nodes,
)

_register(
    name="list_namespaces",
    description="List all namespaces in the cluster.",
    parameters={
        "type": "object",
        "properties": {},
    },
    fn=list_namespaces,
)

# ── Cluster-wide tools ───────────────────────────────────────────────────────

_register(
    name="get_cluster_health",
    description=(
        "Get a cluster-wide health snapshot: node readiness, pod counts per namespace "
        "(running vs total, restart counts), and number of active incidents. "
        "Use this as the first tool for any general health or status question."
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
    fn=get_cluster_health,
)

_register(
    name="list_services",
    description="List all Kubernetes services in a namespace with their type (ClusterIP/NodePort/LoadBalancer), ports, and selectors.",
    parameters={
        "type": "object",
        "properties": {
            "namespace": {"type": "string", "description": "The Kubernetes namespace."},
        },
        "required": ["namespace"],
    },
    fn=list_services,
)

_register(
    name="get_resource_pressure",
    description=(
        "Show top pods by memory and CPU usage as a percentage of their configured limits. "
        "Highlights anything above 80%%. "
        "Use this to answer questions like 'is anything running out of memory?' or 'what is using the most CPU?'. "
        "Omit namespace for cluster-wide view."
    ),
    parameters={
        "type": "object",
        "properties": {
            "namespace": {"type": "string", "description": "Namespace to scope the query (optional)."},
        },
    },
    fn=get_resource_pressure,
)

# ── Prometheus tools ─────────────────────────────────────────────────────────

_register(
    name="query_prometheus",
    description=(
        "Execute a PromQL query against the Prometheus server. "
        "Useful for checking resource usage, replica counts, restart rates, etc. "
        "Available metrics include kube_pod_*, kube_deployment_*, kube_endpoint_*, "
        "container_memory_*, container_cpu_*."
    ),
    parameters={
        "type": "object",
        "properties": {
            "promql": {"type": "string", "description": "The PromQL query string."},
        },
        "required": ["promql"],
    },
    fn=query_prometheus,
)


# ═════════════════════════════════════════════════════════════════════════════
# Public API
# ═════════════════════════════════════════════════════════════════════════════

def get_declarations() -> list[genai_types.FunctionDeclaration]:
    """All Gemini FunctionDeclarations for the tool belt."""
    return [decl for decl, _ in _TOOL_DEFS]


def get_executor() -> dict[str, Callable[..., str]]:
    """Map of tool_name → callable for dispatching function calls."""
    return {decl.name: fn for decl, fn in _TOOL_DEFS}
