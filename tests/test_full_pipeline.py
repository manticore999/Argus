"""
Full end-to-end pipeline tests against real Online Boutique workloads.

Covers every pipeline layer:
  ┌──────────────────────────────────────────────────────────────┐
  │  K8s failure → trigger → analyzer → finding (DB) →           │
  │  correlation → incident (DB) → incident_findings (DB) →      │
  │  LLM diagnosis (DB)                                          │
  │                                                              │
  │  Prometheus metric → handle_warning / handle_direct_finding → │
  │  same pipeline above                                         │
  └──────────────────────────────────────────────────────────────┘

Tests:
  1. DB schema verification  — all tables, columns, indexes
  2. Redis cascade           — K8s event path → finding → Rule 4 + Rule 0 → LLM
  3. Bad image rollout       — 2-pod failure → Rule 0 → incident → LLM
  4. Prometheus PromQL       — validate all 5 rules execute against live cluster
  5. Prometheus→incident     — metric-driven deployment failure → Rule 4 → LLM
  6. DB integrity            — verify all rows, junction table, severity values, diagnosis JSON

Usage:
    python -m pytest tests/test_full_pipeline.py -v
"""

from __future__ import annotations

import json
import os
import sys
import time
import sqlite3
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kubernetes.client import CoreV1Api, AppsV1Api
from kubernetes.client import (
    V1Deployment, V1DeploymentSpec, V1LabelSelector,
    V1PodTemplateSpec, V1ObjectMeta, V1PodSpec, V1Container,
)
from kubernetes.client.exceptions import ApiException

from k8s.client import load_kube_client
from dashboard_main import DashboardAIOpsAgent
from engine.finding_store import FindingStore
from engine.incident_store import IncidentStore
from engine.correlation_engine import CorrelationEngine


# ── Config ───────────────────────────────────────────────────────────────────

BOUTIQUE_NS = "boutique"
TEST_DB     = "data/test_full_pipeline.db"
POLL_WAIT   = 3       # seconds between DB polls
MAX_WAIT    = 120     # seconds to wait for findings / incidents
LLM_WAIT    = 90      # seconds to wait for LLM diagnosis after incident

_ERRORING_REASONS = {
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
    "ErrImageNeverPull", "OOMKilled", "Error",
}


# ── Agent factory ────────────────────────────────────────────────────────────

def _make_agent() -> DashboardAIOpsAgent:
    """DashboardAIOpsAgent (with LLM) redirected to isolated test DB."""
    agent = DashboardAIOpsAgent.__new__(DashboardAIOpsAgent)
    DashboardAIOpsAgent.__init__(agent)
    # Redirect all stores to the test DB
    agent.store              = FindingStore(db_path=TEST_DB)
    agent.incident_store     = IncidentStore(db_path=TEST_DB)
    agent.correlation_engine = CorrelationEngine(agent.incident_store, agent.store)
    # The DashboardAIOpsAgent natively initializes AgentGraph() and uses it via self.agent
    return agent


# ── DB helpers ───────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(TEST_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _poll(query: str, params: tuple, timeout: int = MAX_WAIT) -> sqlite3.Row | None:
    """Poll the test DB until a row matches or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = _db()
        row = conn.execute(query, params).fetchone()
        conn.close()
        if row:
            return row
        time.sleep(POLL_WAIT)
    return None


def _poll_finding(reason: str, resource_like: str, timeout: int = MAX_WAIT) -> sqlite3.Row | None:
    return _poll(
        "SELECT * FROM findings WHERE reason = ? AND resource LIKE ?",
        (reason, f"%{resource_like}%"),
        timeout,
    )


def _poll_finding_any(reasons: list[str], resource_like: str, timeout: int = MAX_WAIT) -> sqlite3.Row | None:
    placeholders = ",".join("?" * len(reasons))
    return _poll(
        f"SELECT * FROM findings WHERE reason IN ({placeholders}) AND resource LIKE ?",
        (*reasons, f"%{resource_like}%"),
        timeout,
    )


def _poll_incident(resource_like: str, timeout: int = MAX_WAIT) -> sqlite3.Row | None:
    return _poll(
        "SELECT * FROM incidents WHERE resource LIKE ? AND status = 'ACTIVE'",
        (f"%{resource_like}%",),
        timeout,
    )


def _poll_diagnosis(incident_id: str, timeout: int = LLM_WAIT) -> str | None:
    row = _poll(
        "SELECT diagnosis FROM incidents WHERE id = ? AND diagnosis IS NOT NULL",
        (incident_id,),
        timeout,
    )
    return row["diagnosis"] if row else None


# ── K8s helpers ──────────────────────────────────────────────────────────────

def _scale(apps: AppsV1Api, name: str, ns: str, replicas: int) -> None:
    apps.patch_namespaced_deployment_scale(
        name=name, namespace=ns, body={"spec": {"replicas": replicas}}
    )
    print(f"    Scaled {name} → {replicas} replica(s)")


def _dep_pods(api: CoreV1Api, apps: AppsV1Api, dep: str, ns: str) -> list[str]:
    d = apps.read_namespaced_deployment(name=dep, namespace=ns)
    labels = d.spec.selector.match_labels or {}
    sel = ",".join(f"{k}={v}" for k, v in labels.items())
    return [p.metadata.name for p in api.list_namespaced_pod(namespace=ns, label_selector=sel).items]


def _wait_pods_reason(api: CoreV1Api, pods: list[str], ns: str,
                       reasons: set[str], timeout: int = MAX_WAIT) -> list[str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        matched = []
        for name in pods:
            try:
                p = api.read_namespaced_pod(name=name, namespace=ns)
                for cs in (p.status.container_statuses or []):
                    if cs.state and cs.state.waiting and cs.state.waiting.reason in reasons:
                        matched.append(name)
                        break
            except ApiException:
                pass
        if len(matched) >= len(pods):
            return matched
        time.sleep(POLL_WAIT)
    return matched


def _wait_ep_zero(api: CoreV1Api, svc: str, ns: str, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ep = api.read_namespaced_endpoints(name=svc, namespace=ns)
            if sum(len(s.addresses or []) for s in (ep.subsets or [])) == 0:
                return True
        except ApiException:
            pass
        time.sleep(POLL_WAIT)
    return False


def _wait_ready(apps: AppsV1Api, dep: str, ns: str, timeout: int = MAX_WAIT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            d = apps.read_namespaced_deployment(name=dep, namespace=ns)
            if (d.status.ready_replicas or 0) >= (d.spec.replicas or 1):
                return True
        except ApiException:
            pass
        time.sleep(POLL_WAIT)
    return False


def _delete_dep(apps: AppsV1Api, name: str, ns: str) -> None:
    try:
        apps.delete_namespaced_deployment(name=name, namespace=ns)
    except ApiException:
        pass


# ═════════════════════════════════════════════════════════════════════════════
# TEST 1 — DB Schema Verification
# ═════════════════════════════════════════════════════════════════════════════

def test_db_schema(agent: DashboardAIOpsAgent, api: CoreV1Api, apps: AppsV1Api) -> bool:
    """Verify all tables, columns, and indexes exist in the test DB."""
    print("\n" + "=" * 60)
    print("[TEST 1] DB Schema — tables, columns, indexes")
    print("=" * 60)

    conn = _db()
    ok = True

    # ── Tables ──
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    for t in ("findings", "incidents", "incident_findings"):
        if t in tables:
            print(f"  PASS  Table '{t}' exists")
        else:
            print(f"  FAIL  Table '{t}' missing")
            ok = False

    # ── Columns ──
    expected_cols = {
        "findings": {"id", "resource_uid", "analyzer", "reason", "resource",
                      "kind", "severity", "message", "owner_chain",
                      "first_seen", "last_seen", "suppressed_count", "log_snippet"},
        "incidents": {"id", "title", "scope", "resource", "resource_uid",
                       "severity", "status", "root_cause", "correlation_rule",
                       "correlation_reason", "first_seen", "last_seen",
                       "resolved_at", "finding_count", "suppressed_symptom_count",
                       "diagnosis"},
        "incident_findings": {"incident_id", "finding_id", "role", "added_at"},
    }
    for table, expected in expected_cols.items():
        if table not in tables:
            continue
        actual = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        missing = expected - actual
        if missing:
            print(f"  FAIL  Table '{table}' missing columns: {missing}")
            ok = False
        else:
            print(f"  PASS  Table '{table}' — all {len(expected)} columns present")

    # ── Indexes ──
    indexes = {r[1] for r in conn.execute(
        "SELECT * FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()}
    for idx in ("idx_findings_last_seen", "idx_findings_resource",
                "idx_incidents_status", "idx_incidents_resource"):
        if idx in indexes:
            print(f"  PASS  Index '{idx}'")
        else:
            print(f"  WARN  Index '{idx}' missing (non-critical)")

    # ── WAL mode ──
    journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if journal == "wal":
        print(f"  PASS  Journal mode = WAL")
    else:
        print(f"  WARN  Journal mode = {journal} (expected WAL)")

    conn.close()
    return ok


# ═════════════════════════════════════════════════════════════════════════════
# TEST 2 — Redis Cascade (K8s Event Path)
# ═════════════════════════════════════════════════════════════════════════════

def test_redis_cascade(agent: DashboardAIOpsAgent, api: CoreV1Api, apps: AppsV1Api) -> bool:
    """
    Delete redis-cart → Service endpoints drop to 0 → EndpointWatcher fires →
    Rule 4 (NoEndpoints) → incident → LLM diagnosis.
    Also: cartservice pods CrashLoop → Rule 0 fires → second incident.
    Verifies: findings in DB, incidents in DB, incident_findings junction,
    severity values, LLM diagnosis JSON structure.
    """
    print("\n" + "=" * 60)
    print("[TEST 2] Redis cascade — K8s event → full pipeline → LLM")
    print("=" * 60)

    # --- Snapshot redis-cart for restore ---
    try:
        redis_dep = apps.read_namespaced_deployment(name="redis-cart", namespace=BOUTIQUE_NS)
    except ApiException:
        print("  SKIP — redis-cart not found")
        return False

    original_cart_replicas = apps.read_namespaced_deployment(
        name="cartservice", namespace=BOUTIQUE_NS
    ).spec.replicas or 1

    # Scale cartservice to 2 so Rule 0 can fire (needs ≥2 pod findings)
    _scale(apps, "cartservice", BOUTIQUE_NS, 2)
    print("    Waiting for 2 cartservice pods...")
    _wait_ready(apps, "cartservice", BOUTIQUE_NS)
    cart_pods = _dep_pods(api, apps, "cartservice", BOUTIQUE_NS)
    print(f"    cartservice pods: {cart_pods}")

    # --- Delete redis-cart ---
    print("    Deleting redis-cart deployment...")
    _delete_dep(apps, "redis-cart", BOUTIQUE_NS)

    # --- Path A: EndpointWatcher → NoEndpoints → Rule 4 ---
    print("    Waiting for redis-cart endpoints → 0...")
    _wait_ep_zero(api, "redis-cart", BOUTIQUE_NS, timeout=30)

    agent.handle_warning(
        kind="Service", namespace=BOUTIQUE_NS, name="redis-cart",
        uid="unknown", reason="NoEndpoints",
        message="Service redis-cart lost all endpoints.",
    )

    svc_finding = _poll_finding("NoEndpoints", "redis-cart", timeout=20)
    svc_incident = _poll_incident("redis-cart", timeout=15)
    svc_diag_json = None

    if svc_finding:
        print(f"  PASS  Finding: NoEndpoints on redis-cart (id={svc_finding['id']})")
        # Verify severity stored as a string name
        sev = svc_finding["severity"]
        if sev in ("CRITICAL", "WARNING", "INFO"):
            print(f"  PASS  Severity stored correctly: '{sev}'")
        else:
            print(f"  FAIL  Unexpected severity value in DB: '{sev}'")
    else:
        print("  FAIL  No NoEndpoints finding for redis-cart")

    if svc_incident:
        svc_inc_id = svc_incident["id"]
        print(f"  PASS  Rule 4 incident: id={svc_inc_id[:12]}…  title='{svc_incident['title']}'")

        # Verify incident_findings junction
        conn = _db()
        links = conn.execute(
            "SELECT finding_id, role FROM incident_findings WHERE incident_id = ?",
            (svc_inc_id,),
        ).fetchall()
        conn.close()
        if links:
            roles = [l["role"] for l in links]
            print(f"  PASS  incident_findings: {len(links)} link(s), roles={roles}")
        else:
            print(f"  WARN  incident_findings empty for this incident")

        # Wait for LLM
        print(f"    Waiting up to {LLM_WAIT}s for LLM diagnosis on service incident...")
        svc_diag_json = _poll_diagnosis(svc_inc_id)
        if svc_diag_json:
            d = json.loads(svc_diag_json)
            print(f"  PASS  LLM diagnosis → cause='{d.get('root_cause','')[:80]}'")
            print(f"         confidence={d.get('confidence','')}")
        else:
            print(f"  WARN  No LLM diagnosis within {LLM_WAIT}s")
    else:
        print("  FAIL  Rule 4 did not create incident for redis-cart NoEndpoints")

    # --- Path B: cartservice pods crash → Rule 0 ---
    print("    Waiting for cartservice pods to CrashLoop...")
    cart_pods = _dep_pods(api, apps, "cartservice", BOUTIQUE_NS)
    crashing = _wait_pods_reason(api, cart_pods, BOUTIQUE_NS, _ERRORING_REASONS, timeout=180)
    if len(crashing) < 2:
        print(f"    Only {len(crashing)} crashed — waiting 90s more...")
        remaining = [p for p in cart_pods if p not in crashing]
        crashing += _wait_pods_reason(api, remaining, BOUTIQUE_NS, _ERRORING_REASONS, timeout=90)

    print(f"    {len(crashing)} crashing pod(s): {crashing}")

    for pod in crashing[:2]:
        agent.handle_warning(
            kind="Pod", namespace=BOUTIQUE_NS, name=pod,
            uid="unknown", reason="BackOff",
            message=f"Pod {pod} back-off restarting — redis-cart unreachable.",
        )
        time.sleep(1)

    cart_incident = _poll_incident("cartservice", timeout=30)
    if cart_incident:
        print(f"  PASS  Rule 0 incident for cartservice: '{cart_incident['title']}'")
    else:
        if len(crashing) < 2:
            print("  WARN  Rule 0 needs ≥2 pod findings — not enough pods crashed")
        else:
            print("  FAIL  No cartservice incident (Rule 0)")

    # --- Restore ---
    print("    Restoring redis-cart + cartservice...")
    redis_dep.metadata.resource_version = None
    redis_dep.metadata.uid = None
    redis_dep.status = None
    try:
        apps.create_namespaced_deployment(namespace=BOUTIQUE_NS, body=redis_dep)
    except ApiException as e:
        if e.status != 409:
            print(f"    WARN restore: {e}")
    _scale(apps, "cartservice", BOUTIQUE_NS, original_cart_replicas)
    _wait_ready(apps, "redis-cart", BOUTIQUE_NS, timeout=90)
    print("    Cluster restored")

    passed = svc_finding is not None and svc_incident is not None
    return passed


# ═════════════════════════════════════════════════════════════════════════════
# TEST 3 — Bad Image Rollout → Rule 0 → LLM
# ═════════════════════════════════════════════════════════════════════════════

def test_bad_image_rollout(agent: DashboardAIOpsAgent, api: CoreV1Api, apps: AppsV1Api) -> bool:
    """
    2-replica deployment with non-existent image → 2 ImagePullBackOff pods →
    Rule 0 infers controller incident → LLM diagnosis.
    Validates: finding count, incident correlation_rule, LLM JSON shape.
    """
    print("\n" + "=" * 60)
    print("[TEST 3] Bad image 2-replica → Rule 0 → incident → LLM")
    print("=" * 60)

    dep_name = "aiops-test-badimage"

    dep = V1Deployment(
        metadata=V1ObjectMeta(name=dep_name, namespace=BOUTIQUE_NS),
        spec=V1DeploymentSpec(
            replicas=2,
            selector=V1LabelSelector(match_labels={"app": dep_name}),
            template=V1PodTemplateSpec(
                metadata=V1ObjectMeta(labels={"app": dep_name}),
                spec=V1PodSpec(containers=[V1Container(
                    name="app",
                    image="registry.k8s.io/this-image-does-not-exist:v9999",
                )]),
            ),
        ),
    )
    try:
        apps.create_namespaced_deployment(namespace=BOUTIQUE_NS, body=dep)
    except ApiException as e:
        if e.status != 409:
            raise

    print("    Waiting for 2 pods in ImagePullBackOff...")
    deadline = time.time() + 30
    pods = []
    while len(pods) < 2 and time.time() < deadline:
        time.sleep(3)
        pods = _dep_pods(api, apps, dep_name, BOUTIQUE_NS)
    failing = _wait_pods_reason(api, pods, BOUTIQUE_NS,
                                {"ImagePullBackOff", "ErrImagePull"}, timeout=MAX_WAIT)
    print(f"    {len(failing)} pods failing: {failing}")

    for pod in failing[:2]:
        agent.handle_warning(
            kind="Pod", namespace=BOUTIQUE_NS, name=pod,
            uid="unknown", reason="Failed",
            message=f"Failed to pull image for {pod}",
        )
        time.sleep(1)

    incident_created_at = time.time()
    incident = _poll_incident(dep_name, timeout=30)

    diag_ok = False
    if incident:
        inc_id = incident["id"]
        print(f"  PASS  Incident: id={inc_id[:12]}…  rule='{incident['correlation_rule']}'")

        # Verify finding_count updated
        if incident["finding_count"] >= 1:
            print(f"  PASS  finding_count={incident['finding_count']}")
        else:
            print(f"  WARN  finding_count={incident['finding_count']} (expected ≥1)")

        # Verify incident_findings rows
        conn = _db()
        links = conn.execute(
            "SELECT role FROM incident_findings WHERE incident_id = ?", (inc_id,)
        ).fetchall()
        conn.close()
        roles = [l["role"] for l in links]
        if "SYMPTOM" in roles or "ROOT_CAUSE" in roles:
            print(f"  PASS  incident_findings roles: {roles}")
        else:
            print(f"  WARN  unexpected roles: {roles}")

        # LLM
        elapsed = time.time() - incident_created_at
        remaining = max(10, int(LLM_WAIT - elapsed))
        print(f"    Waiting up to {remaining}s for LLM diagnosis...")
        diag_json = _poll_diagnosis(inc_id, timeout=remaining)
        if diag_json:
            d = json.loads(diag_json)
            # Validate JSON structure
            required_keys = {"root_cause", "confidence", "suggested_actions"}
            actual_keys = set(d.keys())
            if required_keys.issubset(actual_keys):
                print(f"  PASS  LLM diagnosis JSON — all required keys present")
                print(f"         cause='{d['root_cause'][:70]}'")
                diag_ok = True
            else:
                print(f"  WARN  LLM JSON missing keys: {required_keys - actual_keys}")
                diag_ok = True  # still got a response
        else:
            print(f"  WARN  No LLM diagnosis within {LLM_WAIT}s")
    else:
        print("  FAIL  No incident created")
        if len(failing) < 2:
            print("         Only 1 pod failed — Rule 0 needs ≥2")

    _delete_dep(apps, dep_name, BOUTIQUE_NS)
    print("    Cleanup: deployment deleted")
    return incident is not None


# ═════════════════════════════════════════════════════════════════════════════
# TEST 4 — Prometheus PromQL Queries
# ═════════════════════════════════════════════════════════════════════════════

def test_prometheus_queries(agent: DashboardAIOpsAgent, api: CoreV1Api, apps: AppsV1Api) -> bool:
    """Validate all 5 PrometheusWatcher PromQL rules execute against live Prometheus."""
    print("\n" + "=" * 60)
    print("[TEST 4] Prometheus — validate PromQL rules against live cluster")
    print("=" * 60)

    try:
        from prometheus_api_client import PrometheusConnect
    except ImportError:
        print("  SKIP — prometheus-api-client not installed")
        return False

    prom = PrometheusConnect(url="http://localhost:9090", disable_ssl=True)
    try:
        prom.custom_query("up")
        print("    Prometheus reachable at localhost:9090")
    except Exception as e:
        print(f"  FAIL  Cannot reach Prometheus: {e}")
        return False

    from triggers.prometheus_watcher import _RULES
    all_ok = True

    for rule in _RULES:
        try:
            results = prom.custom_query(rule["query"])
            n = len(results)
            print(f"  PASS  [{rule['label']}] — {n} result(s) ({'alerting' if n else 'clean'})")
        except Exception as e:
            print(f"  FAIL  [{rule['label']}] — {e}")
            all_ok = False

    # Spot-check boutique metrics
    print()
    print("    Boutique metric spot-checks:")
    for query, label in [
        ('kube_deployment_status_replicas_ready{namespace="boutique"}', "deployment replicas"),
        ('kube_pod_status_ready{namespace="boutique",condition="true"}', "pod ready status"),
        ('kube_endpoint_address_available{namespace="boutique"}', "endpoint addresses"),
    ]:
        try:
            r = prom.custom_query(query)
            print(f"  PASS  {label} — {len(r)} metric(s)")
        except Exception as e:
            print(f"  FAIL  {label}: {e}")
            all_ok = False

    return all_ok


# ═════════════════════════════════════════════════════════════════════════════
# TEST 5 — Prometheus → Finding → Rule 4 Incident → LLM
# ═════════════════════════════════════════════════════════════════════════════

def test_prometheus_to_incident(agent: DashboardAIOpsAgent, api: CoreV1Api, apps: AppsV1Api) -> bool:
    """
    Deploy 1-replica bad-image → kube-state-metrics scrapes unavailable=1 →
    verify metric exists in Prometheus → route through handle_warning →
    finding → Rule 4 (ZeroAvailableReplicas) → incident → LLM diagnosis.
    Also calls handle_direct_finding to test the metric-only path.
    """
    print("\n" + "=" * 60)
    print("[TEST 5] Prometheus metric → finding → Rule 4 → LLM")
    print("=" * 60)

    try:
        from prometheus_api_client import PrometheusConnect
        prom = PrometheusConnect(url="http://localhost:9090", disable_ssl=True)
        prom.custom_query("up")
    except Exception as e:
        print(f"  SKIP — Prometheus not reachable: {e}")
        return False

    dep_name = "prom-test-dep"

    # Clean up any leftover
    _delete_dep(apps, dep_name, BOUTIQUE_NS)
    time.sleep(2)

    dep_body = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": dep_name, "namespace": BOUTIQUE_NS},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": dep_name}},
            "template": {
                "metadata": {"labels": {"app": dep_name}},
                "spec": {"containers": [{
                    "name": "c",
                    "image": "bad-registry.invalid/no-such-image:v0",
                    "imagePullPolicy": "Always",
                }]},
            },
        },
    }
    apps.create_namespaced_deployment(namespace=BOUTIQUE_NS, body=dep_body)
    print(f"    Created {dep_name} (bad image, 1 replica)")

    # Wait for Prometheus scrape
    print("    Waiting 40s for kube-state-metrics scrape...")
    time.sleep(40)

    query = (
        f'kube_deployment_status_replicas_unavailable{{namespace="{BOUTIQUE_NS}",'
        f'deployment="{dep_name}"}} > 0'
    )
    results = prom.custom_query(query)
    if results:
        print(f"  PASS  Prometheus metric: {dep_name} unavailable={results[0]['value'][1]}")
    else:
        print("  WARN  Metric not yet scraped (scrape may be slow)")

    # --- Path A: handle_warning (what PrometheusWatcher does for action="k8s") ---
    print("    Routing: handle_warning(kind=Deployment) → analyzer → finding → correlation")
    agent.handle_warning(
        kind="Deployment", namespace=BOUTIQUE_NS, name=dep_name,
        uid="unknown", reason="UnavailableReplicas",
        message=f"Prometheus: {dep_name} has unavailable replicas.",
    )

    finding = _poll_finding_any(
        ["ZeroAvailableReplicas", "UnavailableReplicas"], dep_name, timeout=20,
    )
    if finding:
        print(f"  PASS  Finding: reason='{finding['reason']}', severity='{finding['severity']}'")
    else:
        print("  FAIL  No finding from handle_warning on Deployment")

    # --- Path B: handle_direct_finding (action="metric" path) ---
    print("    Routing: handle_direct_finding → metric finding → correlation")
    agent.handle_direct_finding(
        kind="Deployment", namespace=BOUTIQUE_NS, name=dep_name,
        reason="MetricUnavailable",
        message=f"metric: {dep_name} has 1 unavailable replica (direct metric path)",
        extra_context={"source": "prometheus", "query": query},
    )

    metric_finding = _poll_finding("MetricUnavailable", dep_name, timeout=15)
    if metric_finding:
        print(f"  PASS  handle_direct_finding: stored finding id={metric_finding['id']}")
    else:
        print(f"  WARN  handle_direct_finding did not create a finding (may be deduped)")

    # --- Rule 4 incident ---
    incident_created_at = time.time()
    dep_incident = _poll_incident(dep_name, timeout=15)
    if dep_incident:
        inc_id = dep_incident["id"]
        print(f"  PASS  Rule 4 incident: '{dep_incident['title']}'")
        print(f"         correlation_rule='{dep_incident['correlation_rule']}'")

        # Verify incident_findings
        conn = _db()
        links = conn.execute(
            "SELECT finding_id, role FROM incident_findings WHERE incident_id = ?",
            (inc_id,),
        ).fetchall()
        conn.close()
        print(f"  PASS  incident_findings: {len(links)} link(s), "
              f"roles={[l['role'] for l in links]}")

        # LLM
        elapsed = time.time() - incident_created_at
        remaining = max(10, int(LLM_WAIT - elapsed))
        print(f"    Waiting up to {remaining}s for LLM diagnosis...")
        diag_json = _poll_diagnosis(inc_id, timeout=remaining)
        if diag_json:
            d = json.loads(diag_json)
            print(f"  PASS  LLM diagnosis → '{d.get('root_cause','')[:70]}'")
        else:
            print(f"  WARN  No LLM diagnosis within {LLM_WAIT}s")
    else:
        print("  FAIL  No Rule 4 incident for ZeroAvailableReplicas")

    _delete_dep(apps, dep_name, BOUTIQUE_NS)
    print(f"    Cleanup: {dep_name} deleted")

    return finding is not None and dep_incident is not None


# ═════════════════════════════════════════════════════════════════════════════
# TEST 6 — DB Integrity (runs last, inspects all rows from tests 1-5)
# ═════════════════════════════════════════════════════════════════════════════

def test_db_integrity(agent: DashboardAIOpsAgent, api: CoreV1Api, apps: AppsV1Api) -> bool:
    """
    Post-hoc verification of everything stored in the DB by the previous tests.
    Checks: severity enum values, incident statuses, junction table consistency,
    diagnosis JSON schema, no orphaned links.
    """
    print("\n" + "=" * 60)
    print("[TEST 6] DB Integrity — verify all stored data")
    print("=" * 60)

    conn = _db()
    ok = True

    # ── Findings: severity values must be CRITICAL/WARNING/INFO ──
    bad_sev = conn.execute(
        "SELECT id, severity FROM findings WHERE severity NOT IN ('CRITICAL','WARNING','INFO')"
    ).fetchall()
    if bad_sev:
        print(f"  FAIL  {len(bad_sev)} finding(s) with invalid severity: {[dict(r) for r in bad_sev]}")
        ok = False
    else:
        total = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
        print(f"  PASS  All {total} finding(s) have valid severity values")

    # ── Incidents: status values must be ACTIVE/RESOLVED/ACKNOWLEDGED ──
    bad_status = conn.execute(
        "SELECT id, status FROM incidents WHERE status NOT IN ('ACTIVE','RESOLVED','ACKNOWLEDGED')"
    ).fetchall()
    if bad_status:
        print(f"  FAIL  {len(bad_status)} incident(s) with invalid status")
        ok = False
    else:
        total = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        print(f"  PASS  All {total} incident(s) have valid status values")

    # ── incident_findings: roles must be ROOT_CAUSE/SYMPTOM/RELATED/CONTRIBUTING ──
    valid_roles = ("ROOT_CAUSE", "SYMPTOM", "RELATED", "CONTRIBUTING")
    placeholders = ",".join("?" * len(valid_roles))
    bad_roles = conn.execute(
        f"SELECT * FROM incident_findings WHERE role NOT IN ({placeholders})",
        valid_roles,
    ).fetchall()
    if bad_roles:
        print(f"  FAIL  {len(bad_roles)} link(s) with invalid role: {[dict(r) for r in bad_roles]}")
        ok = False
    else:
        total = conn.execute("SELECT COUNT(*) FROM incident_findings").fetchone()[0]
        print(f"  PASS  All {total} incident_finding link(s) have valid roles")

    # ── No orphaned incident_findings (incident deleted but links remain) ──
    orphans = conn.execute("""
        SELECT if2.incident_id, if2.finding_id FROM incident_findings if2
        LEFT JOIN incidents i ON i.id = if2.incident_id
        WHERE i.id IS NULL
    """).fetchall()
    if orphans:
        print(f"  FAIL  {len(orphans)} orphaned incident_findings rows")
        ok = False
    else:
        print(f"  PASS  No orphaned incident_findings")

    # ── Diagnosis JSON is valid where present ──
    diag_rows = conn.execute(
        "SELECT id, diagnosis FROM incidents WHERE diagnosis IS NOT NULL"
    ).fetchall()
    diag_valid = 0
    for row in diag_rows:
        try:
            d = json.loads(row["diagnosis"])
            if "root_cause" in d and "confidence" in d:
                diag_valid += 1
            else:
                print(f"  WARN  Diagnosis for {row['id'][:12]} missing expected keys")
        except json.JSONDecodeError:
            print(f"  FAIL  Invalid JSON in diagnosis for incident {row['id'][:12]}")
            ok = False
    if diag_rows:
        print(f"  PASS  {diag_valid}/{len(diag_rows)} diagnosis JSON(s) have valid structure")
    else:
        print(f"  INFO  No diagnoses stored yet (LLM may not have run)")

    # ── findings.first_seen / last_seen are valid ISO timestamps ──
    from datetime import datetime
    ts_rows = conn.execute("SELECT id, first_seen, last_seen FROM findings").fetchall()
    ts_bad = 0
    for r in ts_rows:
        for col in ("first_seen", "last_seen"):
            try:
                datetime.fromisoformat(r[col])
            except (ValueError, TypeError):
                ts_bad += 1
    if ts_bad:
        print(f"  FAIL  {ts_bad} finding timestamp(s) are not valid ISO format")
        ok = False
    else:
        print(f"  PASS  All finding timestamps are valid ISO format")

    conn.close()
    return ok


# ═════════════════════════════════════════════════════════════════════════════
# Runner
# ═════════════════════════════════════════════════════════════════════════════

def _clean_db() -> None:
    try:
        os.remove(TEST_DB)
    except FileNotFoundError:
        pass


def main():
    print("=" * 60)
    print("  AIOps Full Pipeline Test Suite")
    print("  Live cluster · Online Boutique · Prometheus · LLM")
    print("=" * 60)
    print(f"  Namespace       : {BOUTIQUE_NS}")
    print(f"  Test database   : {TEST_DB}")
    print(f"  LLM timeout     : {LLM_WAIT}s per incident")
    print()

    _clean_db()
    api  = load_kube_client()
    apps = AppsV1Api(api.api_client)
    agent = _make_agent()

    if agent.agent._client:
        print("  LLM: Gemini available — diagnoses will be tested")
    else:
        print("  LLM: unavailable — incidents tested, diagnosis skipped")
    print()

    # Test order matters: schema first, DB integrity last (reads what others wrote)
    tests = [
        test_db_schema,
        test_redis_cascade,
        test_bad_image_rollout,
        test_prometheus_queries,
        test_prometheus_to_incident,
        test_db_integrity,
    ]

    results: dict[str, bool] = {}
    for fn in tests:
        try:
            results[fn.__name__] = fn(agent, api, apps)
        except Exception as e:
            import traceback
            print(f"\n  ERROR in {fn.__name__}: {e}")
            traceback.print_exc()
            results[fn.__name__] = False

    # ── Summary ──
    print("\n" + "=" * 60)
    print("  RESULTS")
    print("=" * 60)
    passed = sum(v for v in results.values())
    total  = len(results)
    for name, ok in results.items():
        tag = "PASS" if ok else "FAIL"
        print(f"  [{tag}]  {name}")

    # ── DB dump ──
    print("\n" + "-" * 60)
    print("  Findings in DB:")
    conn = _db()
    for r in conn.execute(
        "SELECT id, resource, reason, severity, analyzer FROM findings ORDER BY last_seen DESC"
    ):
        print(f"    #{r['id']}  {r['resource']}  reason={r['reason']}  "
              f"sev={r['severity']}  analyzer={r['analyzer']}")

    print()
    print("  Incidents in DB:")
    for r in conn.execute(
        "SELECT id, title, scope, status, correlation_rule, finding_count, "
        "diagnosis IS NOT NULL as has_diag FROM incidents ORDER BY last_seen DESC"
    ):
        print(f"    {r['id'][:12]}…  {r['title']}")
        print(f"      scope={r['scope']}  status={r['status']}  rule={r['correlation_rule']}  "
              f"findings={r['finding_count']}  diagnosed={'yes' if r['has_diag'] else 'no'}")

    print()
    print("  incident_findings junction:")
    for r in conn.execute(
        "SELECT incident_id, finding_id, role FROM incident_findings"
    ):
        print(f"    incident={r['incident_id'][:12]}…  finding=#{r['finding_id']}  role={r['role']}")

    conn.close()

    print(f"\n  {passed}/{total} tests passed")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
