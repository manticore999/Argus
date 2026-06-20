"""
Live pipeline tests — inject real failures into Minikube, verify end-to-end detection.

Each test:
  1. Applies a broken K8s workload to the 'aiops-test' namespace.
  2. Waits until the resource reaches the expected failure state.
  3. Calls agent.handle_warning() (same path the watchers use).
  4. Asserts the expected Finding and/or Incident appeared in the DB.
  5. Cleans up the K8s resource.

Usage:
    python -m pytest tests/test_pipeline_live.py -v
"""

from __future__ import annotations

import os
import sys
import time
import sqlite3

# Allow imports from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kubernetes.client import CoreV1Api, AppsV1Api, V1Pod, V1ObjectMeta, V1PodSpec, V1Container
from kubernetes.client import V1Service, V1ServiceSpec, V1ServicePort, V1Deployment
from kubernetes.client import V1DeploymentSpec, V1LabelSelector, V1PodTemplateSpec
from kubernetes.client.exceptions import ApiException

from k8s.client import load_kube_client
from main import AIOpsAgent


TEST_NS    = "aiops-test"
TEST_DB    = "data/test_pipeline.db"
POLL_WAIT  = 3   # seconds between polls
MAX_WAIT   = 90  # seconds before a test times out


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_agent() -> AIOpsAgent:
    """Create an AIOpsAgent backed by an isolated test database."""
    agent = AIOpsAgent.__new__(AIOpsAgent)
    AIOpsAgent.__init__(agent)
    # Redirect all stores to test DB so production data is untouched
    from engine.finding_store import FindingStore
    from engine.incident_store import IncidentStore
    from engine.correlation_engine import CorrelationEngine

    agent.store           = FindingStore(db_path=TEST_DB)
    agent.incident_store  = IncidentStore(db_path=TEST_DB)
    agent.correlation_engine = CorrelationEngine(agent.incident_store)
    return agent


def _poll_finding(db_path: str, reason: str, resource_like: str, timeout: int = MAX_WAIT) -> dict | None:
    """Poll the findings table until a matching row appears or timeout."""
    conn = sqlite3.connect(db_path)
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = conn.execute(
            "SELECT * FROM findings WHERE reason = ? AND resource LIKE ?",
            (reason, f"%{resource_like}%"),
        ).fetchone()
        if row:
            conn.close()
            return row
        time.sleep(POLL_WAIT)
    conn.close()
    return None


def _poll_incident(db_path: str, scope: str, resource_like: str, timeout: int = MAX_WAIT) -> dict | None:
    """Poll the incidents table until a matching active incident appears or timeout."""
    conn = sqlite3.connect(db_path)
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = conn.execute(
            "SELECT * FROM incidents WHERE scope = ? AND resource LIKE ? AND status = 'ACTIVE'",
            (scope, f"%{resource_like}%"),
        ).fetchone()
        if row:
            conn.close()
            return row
        time.sleep(POLL_WAIT)
    conn.close()
    return None


def _wait_pod_reason(api: CoreV1Api, name: str, reasons: set[str], timeout: int = MAX_WAIT) -> bool:
    """Wait until a pod container has a waiting.reason in the given set."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            pod = api.read_namespaced_pod(name=name, namespace=TEST_NS)
            for cs in (pod.status.container_statuses or []) + (pod.status.init_container_statuses or []):
                if cs.state and cs.state.waiting and cs.state.waiting.reason in reasons:
                    return True
        except ApiException:
            pass
        time.sleep(POLL_WAIT)
    return False


def _wait_endpoint_zero(api: CoreV1Api, svc_name: str, timeout: int = MAX_WAIT) -> bool:
    """Wait until a service's endpoints drop to 0."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ep = api.read_namespaced_endpoints(name=svc_name, namespace=TEST_NS)
            subsets = ep.subsets or []
            count = sum(len(s.addresses or []) for s in subsets)
            if count == 0:
                return True
        except ApiException:
            pass
        time.sleep(POLL_WAIT)
    return False


def _delete_pod(api: CoreV1Api, name: str) -> None:
    try:
        api.delete_namespaced_pod(name=name, namespace=TEST_NS)
    except ApiException:
        pass


def _delete_svc(api: CoreV1Api, name: str) -> None:
    try:
        api.delete_namespaced_service(name=name, namespace=TEST_NS)
    except ApiException:
        pass


def _delete_deployment(apps: AppsV1Api, name: str) -> None:
    try:
        apps.delete_namespaced_deployment(name=name, namespace=TEST_NS)
    except ApiException:
        pass


def _clean_db() -> None:
    try:
        import os
        os.remove(TEST_DB)
    except FileNotFoundError:
        pass


# ── Test cases ────────────────────────────────────────────────────────────────

def test_crashloop(agent: AIOpsAgent, api: CoreV1Api) -> bool:
    """
    Deploy a pod that immediately exits with code 1.
    K8s will restart it until CrashLoopBackOff.
    Pipeline should detect CrashLoopBackOff finding + incident.
    """
    print("\n[TEST 1] CrashLoop detection")
    pod_name = "aiops-test-crashloop"

    pod = V1Pod(
        metadata=V1ObjectMeta(name=pod_name, namespace=TEST_NS),
        spec=V1PodSpec(
            restart_policy="Always",
            containers=[V1Container(
                name="crasher",
                image="busybox:latest",
                command=["sh", "-c", "echo 'crashing'; exit 1"],
            )],
        ),
    )
    try:
        api.create_namespaced_pod(namespace=TEST_NS, body=pod)
    except ApiException as e:
        if e.status != 409:
            raise

    print(f"  Waiting for {pod_name} to enter CrashLoopBackOff...")
    reached = _wait_pod_reason(api, pod_name, {"CrashLoopBackOff"})
    if not reached:
        print("  SKIP — pod did not reach CrashLoopBackOff in time")
        _delete_pod(api, pod_name)
        return False

    print("  Pod is in CrashLoopBackOff — triggering pipeline")
    agent.handle_warning(
        kind="Pod", namespace=TEST_NS, name=pod_name,
        uid="unknown", reason="BackOff", message="Back-off restarting failed container",
    )

    finding = _poll_finding(TEST_DB, "CrashLoopBackOff", pod_name)
    incident = _poll_incident(TEST_DB, "Pod", pod_name)

    _delete_pod(api, pod_name)

    if finding:
        print(f"  PASS  Finding: CrashLoopBackOff on {pod_name}")
    else:
        print(f"  FAIL  No CrashLoopBackOff finding found")

    if incident:
        print(f"  PASS  Incident created: scope=Pod resource={pod_name}")
    else:
        print(f"  WARN  No incident created (may need >=2 findings for correlation)")

    return finding is not None


def test_image_pull(agent: AIOpsAgent, api: CoreV1Api) -> bool:
    """
    Deploy a pod with a non-existent image.
    Pipeline should detect ImagePullBackOff finding.
    """
    print("\n[TEST 2] ImagePull failure detection")
    pod_name = "aiops-test-badimage"

    pod = V1Pod(
        metadata=V1ObjectMeta(name=pod_name, namespace=TEST_NS),
        spec=V1PodSpec(
            restart_policy="Never",
            containers=[V1Container(
                name="bad-image",
                image="registry.k8s.io/this-image-does-not-exist:v9999",
            )],
        ),
    )
    try:
        api.create_namespaced_pod(namespace=TEST_NS, body=pod)
    except ApiException as e:
        if e.status != 409:
            raise

    print(f"  Waiting for {pod_name} to enter ImagePullBackOff / ErrImagePull...")
    reached = _wait_pod_reason(api, pod_name, {"ImagePullBackOff", "ErrImagePull"})
    if not reached:
        print("  SKIP — pod did not reach ImagePull error in time")
        _delete_pod(api, pod_name)
        return False

    print("  Pod is in ImagePullBackOff — triggering pipeline")
    agent.handle_warning(
        kind="Pod", namespace=TEST_NS, name=pod_name,
        uid="unknown", reason="Failed", message="Failed to pull image",
    )

    # Reason may be ErrImagePull (initial attempt) or ImagePullBackOff (after backoff)
    finding = _poll_finding(TEST_DB, "ErrImagePull", pod_name) or \
              _poll_finding(TEST_DB, "ImagePullBackOff", pod_name)
    _delete_pod(api, pod_name)

    if finding:
        print(f"  PASS  Finding: image pull error on {pod_name}")
    else:
        print(f"  FAIL  No image pull finding found")

    return finding is not None


def test_service_no_endpoints(agent: AIOpsAgent, api: CoreV1Api) -> bool:
    """
    Create a service whose selector matches nothing.
    Pipeline should detect NoEndpoints finding via ServiceEndpointMissingAnalyzer.
    """
    print("\n[TEST 3] Service with no endpoints")
    svc_name = "aiops-test-orphan-svc"

    svc = V1Service(
        metadata=V1ObjectMeta(name=svc_name, namespace=TEST_NS),
        spec=V1ServiceSpec(
            selector={"app": "does-not-exist-anywhere"},
            ports=[V1ServicePort(port=80, target_port=8080)],
        ),
    )
    try:
        api.create_namespaced_service(namespace=TEST_NS, body=svc)
    except ApiException as e:
        if e.status != 409:
            raise

    # Wait for endpoints to be 0 (should be immediate)
    _wait_endpoint_zero(api, svc_name, timeout=15)

    print("  Service created with no backends — triggering pipeline")
    agent.handle_warning(
        kind="Service", namespace=TEST_NS, name=svc_name,
        uid="unknown", reason="NoEndpoints", message="No endpoints for service",
    )

    finding = _poll_finding(TEST_DB, "NoEndpoints", svc_name)
    _delete_svc(api, svc_name)

    if finding:
        print(f"  PASS  Finding: NoEndpoints on {svc_name}")
    else:
        print(f"  FAIL  No NoEndpoints finding found")

    return finding is not None


def test_endpoint_watcher_cascade(agent: AIOpsAgent, api: CoreV1Api) -> bool:
    """
    Create a service backed by a healthy pod, then delete the pod.
    Simulate EndpointWatcher detecting the drop and calling handle_warning.
    """
    print("\n[TEST 4] EndpointWatcher — cascade detection (delete backing pod)")
    svc_name  = "aiops-test-cascade-svc"
    pod_name  = "aiops-test-cascade-pod"
    label     = {"app": "aiops-cascade"}

    svc = V1Service(
        metadata=V1ObjectMeta(name=svc_name, namespace=TEST_NS),
        spec=V1ServiceSpec(
            selector=label,
            ports=[V1ServicePort(port=80, target_port=8080)],
        ),
    )
    pod = V1Pod(
        metadata=V1ObjectMeta(name=pod_name, namespace=TEST_NS, labels=label),
        spec=V1PodSpec(
            containers=[V1Container(
                name="app",
                image="busybox:latest",
                command=["sleep", "3600"],
            )],
        ),
    )

    try:
        api.create_namespaced_service(namespace=TEST_NS, body=svc)
    except ApiException as e:
        if e.status != 409:
            raise
    try:
        api.create_namespaced_pod(namespace=TEST_NS, body=pod)
    except ApiException as e:
        if e.status != 409:
            raise

    # Wait for pod to be Running so endpoints are populated
    print(f"  Waiting for {pod_name} to be Running...")
    deadline = time.time() + MAX_WAIT
    running = False
    while time.time() < deadline:
        try:
            p = api.read_namespaced_pod(name=pod_name, namespace=TEST_NS)
            if p.status.phase == "Running":
                running = True
                break
        except ApiException:
            pass
        time.sleep(POLL_WAIT)

    if not running:
        print("  SKIP — backing pod never reached Running state")
        _delete_pod(api, pod_name)
        _delete_svc(api, svc_name)
        return False

    # Now delete the pod — endpoints will drop to 0
    print(f"  Deleting backing pod to simulate cascade...")
    _delete_pod(api, pod_name)
    dropped = _wait_endpoint_zero(api, svc_name)

    if not dropped:
        print("  SKIP — endpoints did not drop to 0 in time")
        _delete_svc(api, svc_name)
        return False

    # Simulate what EndpointWatcher would call
    print("  Endpoints dropped to 0 — EndpointWatcher triggers pipeline")
    agent.handle_warning(
        kind="Service", namespace=TEST_NS, name=svc_name,
        uid="unknown", reason="NoEndpoints",
        message=f"Service '{svc_name}' lost all endpoints (dropped to 0).",
    )

    finding = _poll_finding(TEST_DB, "NoEndpoints", svc_name)
    _delete_svc(api, svc_name)

    if finding:
        print(f"  PASS  Finding: cascade NoEndpoints on {svc_name}")
    else:
        print(f"  FAIL  No cascade finding found")

    return finding is not None


def test_deployment_unavailable(agent: AIOpsAgent, api: CoreV1Api) -> bool:
    """
    Deploy with a bad image so replicas never become available.
    Pipeline should detect ZeroAvailableReplicas finding.
    """
    print("\n[TEST 5] Deployment with unavailable replicas")
    apps     = AppsV1Api(api.api_client)
    dep_name = "aiops-test-bad-deploy"
    label    = {"app": "aiops-bad-deploy"}

    dep = V1Deployment(
        metadata=V1ObjectMeta(name=dep_name, namespace=TEST_NS),
        spec=V1DeploymentSpec(
            replicas=2,
            selector=V1LabelSelector(match_labels=label),
            template=V1PodTemplateSpec(
                metadata=V1ObjectMeta(labels=label),
                spec=V1PodSpec(
                    containers=[V1Container(
                        name="app",
                        image="registry.k8s.io/this-image-does-not-exist:v9999",
                    )],
                ),
            ),
        ),
    )
    try:
        apps.create_namespaced_deployment(namespace=TEST_NS, body=dep)
    except ApiException as e:
        if e.status != 409:
            raise

    # Give K8s a moment to apply then trigger immediately
    time.sleep(5)
    print("  Deployment created with bad image — triggering pipeline")
    agent.handle_warning(
        kind="Deployment", namespace=TEST_NS, name=dep_name,
        uid="unknown", reason="ReplicaSetCreateError",
        message="Deployment has unavailable replicas",
    )

    finding = _poll_finding(TEST_DB, "ZeroAvailableReplicas", dep_name)
    _delete_deployment(apps, dep_name)

    if finding:
        print(f"  PASS  Finding: ZeroAvailableReplicas on {dep_name}")
    else:
        print(f"  FAIL  No ZeroAvailableReplicas finding found")

    return finding is not None


def test_direct_finding_metric(agent: AIOpsAgent, api: CoreV1Api) -> bool:
    """
    Call handle_direct_finding directly (simulates PrometheusWatcher memory_pressure rule).
    Verify the metric finding flows through to the DB without an analyzer snapshot.
    """
    print("\n[TEST 6] Direct metric finding (Prometheus memory_pressure path)")

    agent.handle_direct_finding(
        kind="Pod",
        namespace=TEST_NS,
        name="aiops-test-memory-pod",
        reason="MemoryPressure",
        message=(
            "Container 'app' in pod 'aiops-test-memory-pod' (namespace 'aiops-test') "
            "has sustained >95% memory usage over 5 minutes — OOM kill likely."
        ),
        extra_context={"container": "app"},
    )

    finding = _poll_finding(TEST_DB, "MemoryPressure", "aiops-test-memory-pod", timeout=15)

    if finding:
        print("  PASS  Metric finding persisted via handle_direct_finding")
    else:
        print("  FAIL  Metric finding did not appear in DB")

    return finding is not None


# ── Runner ───────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("AIOps Live Pipeline Test Suite")
    print("=" * 60)

    _clean_db()
    api   = load_kube_client()
    agent = _make_agent()

    tests = [
        test_crashloop,
        test_image_pull,
        test_service_no_endpoints,
        test_endpoint_watcher_cascade,
        test_deployment_unavailable,
        test_direct_finding_metric,
    ]

    results: dict[str, bool] = {}
    for fn in tests:
        try:
            results[fn.__name__] = fn(agent, api)
        except Exception as e:
            import traceback
            print(f"  ERROR in {fn.__name__}: {e}")
            traceback.print_exc()
            results[fn.__name__] = False

    print("\n" + "=" * 60)
    print("Results")
    print("=" * 60)
    passed = sum(v for v in results.values())
    total  = len(results)
    for name, ok in results.items():
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")

    print(f"\n{passed}/{total} tests passed")
    print("=" * 60)

    # Leave test DB for inspection; remove if you want a clean slate
    # _clean_db()
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
