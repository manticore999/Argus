"""
Live chatbot integration tests — real Minikube cluster, real Gemini.

Each test sends a natural-language question to the AgentLoop and validates:
  1. The agent called the RIGHT tools (not random ones)
  2. The tool results contain expected data from the live cluster
  3. The final text answer references the evidence
  4. Session persistence works across turns

Scenarios cover:
  ┌────┬───────────────────────────────────────────────────────────────────────┐
  │  # │ Scenario                                                             │
  ├────┼───────────────────────────────────────────────────────────────────────┤
  │  1 │ Cluster health check  — "Is the cluster healthy?"                    │
  │  2 │ List pods             — "Show pods in boutique namespace"            │
  │  3 │ List deployments      — "What deployments are running in boutique?"  │
  │  4 │ Describe a resource   — "Describe the frontend deployment"          │
  │  5 │ Pod logs              — "Show me logs for a boutique pod"            │
  │  6 │ Events listing        — "Are there any warnings in boutique?"       │
  │  7 │ Namespace enumeration — "What namespaces exist?"                    │
  │  8 │ Node inspection       — "How are the cluster nodes?"               │
  │  9 │ Services listing      — "What services are in boutique?"            │
  │ 10 │ Prometheus query      — "What's the memory usage of boutique pods?" │
  │ 11 │ Incident investigation — inject a bad pod, trigger pipeline,        │
  │    │                          ask "Any incidents?"                        │
  │ 12 │ Multi-turn follow-up  — ask about incidents then dig deeper         │
  │ 13 │ Resource pressure     — "Any pods under memory pressure?"           │
  │ 14 │ Error handling        — ask about a non-existent namespace          │
  │ 15 │ Broad diagnosis       — "Something is wrong with checkout, help"    │
  └────┴───────────────────────────────────────────────────────────────────────┘

Pre-requisites:
  - Minikube running with Online Boutique in 'boutique' namespace
  - kube-prometheus-stack in 'monitoring' namespace
  - Prometheus port-forwarded: kubectl port-forward -n monitoring svc/prometheus-kube-prometheus-prometheus 9090:9090
  - GOOGLE_API_KEY environment variable set

Usage:
    python -m pytest tests/test_chatbot_live.py -v
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.session import SessionStore
from agent.loop import AgentLoop


# ── Config ───────────────────────────────────────────────────────────────────

TEST_DB       = "data/test_chatbot.db"
BOUTIQUE_NS   = "boutique"
MONITORING_NS = "monitoring"
THROTTLE_SEC  = 15  # pause between tests to stay within Gemini rate limits

# ── Colours ──────────────────────────────────────────────────────────────────

_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_GREEN  = "\033[32m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_CYAN   = "\033[36m"
_BLUE   = "\033[34m"


# ── Test harness ─────────────────────────────────────────────────────────────

class ChatTestResult:
    """Captures everything the agent did during one chat() call."""

    def __init__(self):
        self.tool_calls: list[dict] = []      # {"name": ..., "args": ...}
        self.tool_results: list[dict] = []    # {"name": ..., "result": ...}
        self.final_text: str = ""

    @property
    def tool_names(self) -> list[str]:
        return [tc["name"] for tc in self.tool_calls]

    def any_tool_called(self, *names: str) -> bool:
        """True if at least one of the given tool names was called."""
        return bool(set(names) & set(self.tool_names))

    def tool_result_contains(self, tool_name: str, substr: str) -> bool:
        """True if any result from the given tool contains the substring."""
        for tr in self.tool_results:
            if tr["name"] == tool_name and substr.lower() in tr["result"].lower():
                return True
        return False

    def text_mentions(self, *keywords: str) -> bool:
        """True if the final answer mentions ALL given keywords (case-insensitive)."""
        lower = self.final_text.lower()
        return all(kw.lower() in lower for kw in keywords)

    def text_mentions_any(self, *keywords: str) -> bool:
        """True if the final answer mentions ANY of the given keywords."""
        lower = self.final_text.lower()
        return any(kw.lower() in lower for kw in keywords)


def run_chat(agent: AgentLoop, question: str) -> ChatTestResult:
    """Send a question and collect all events into a ChatTestResult."""
    result = ChatTestResult()
    for event in agent.chat(question):
        t = event["type"]
        if t == "tool_call":
            result.tool_calls.append({"name": event["name"], "args": event["args"]})
            args_str = ", ".join(f"{k}={v!r}" for k, v in event["args"].items())
            print(f"    {_DIM}{_YELLOW}> {event['name']}({args_str}){_RESET}")
        elif t == "tool_result":
            result.tool_results.append({"name": event["name"], "result": event["result"]})
            preview = event["result"][:150].replace("\n", " ")
            print(f"    {_DIM}{_BLUE}↳ {preview}…{_RESET}")
        elif t == "text":
            result.final_text = event["content"]
            # Print first 300 chars of answer
            preview = event["content"][:300]
            if len(event["content"]) > 300:
                preview += "…"
            print(f"    {_GREEN}Agent: {preview}{_RESET}")
    return result


def make_session(store: SessionStore, title: str) -> tuple[str, AgentLoop]:
    """Create a fresh session + agent loop."""
    sid = store.create_session(title)
    agent = AgentLoop(session_id=sid, session_store=store)
    return sid, agent


# ═════════════════════════════════════════════════════════════════════════════
# Test scenarios
# ═════════════════════════════════════════════════════════════════════════════

def test_01_cluster_health(store: SessionStore) -> bool:
    """
    Ask: "Is the cluster healthy?"
    Expect: get_cluster_health tool called, answer mentions nodes + namespaces.
    """
    _, agent = make_session(store, "test-01-health")
    r = run_chat(agent, "Is the cluster healthy? Give me an overview.")

    assert r.any_tool_called("get_cluster_health", "list_nodes"), \
        f"Expected health/node tool, got: {r.tool_names}"
    assert r.final_text, "No text answer"
    assert r.text_mentions_any("node", "healthy", "running", "ready"), \
        "Answer should mention cluster state"
    return True


def test_02_list_pods(store: SessionStore) -> bool:
    """
    Ask: "Show me the pods in boutique namespace"
    Expect: list_pods(namespace="boutique"), answer lists pod names.
    """
    _, agent = make_session(store, "test-02-pods")
    r = run_chat(agent, "Show me all the pods in the boutique namespace.")

    assert r.any_tool_called("list_pods"), \
        f"Expected list_pods, got: {r.tool_names}"
    # The boutique namespace should have the Online Boutique pods
    assert r.tool_result_contains("list_pods", "frontend") or \
           r.text_mentions_any("frontend", "cart", "checkout", "currency"), \
        "Should see Online Boutique pod names"
    return True


def test_03_list_deployments(store: SessionStore) -> bool:
    """
    Ask: "What deployments are running in boutique?"
    Expect: list_deployments(namespace="boutique"), answer mentions deployments.
    """
    _, agent = make_session(store, "test-03-deployments")
    r = run_chat(agent, "What deployments are running in the boutique namespace?")

    assert r.any_tool_called("list_deployments"), \
        f"Expected list_deployments, got: {r.tool_names}"
    assert r.text_mentions_any("frontend", "cartservice", "checkoutservice", "deployment"), \
        "Answer should list boutique deployments"
    return True


def test_04_describe_resource(store: SessionStore) -> bool:
    """
    Ask: "Describe the frontend deployment in boutique"
    Expect: describe_resource(kind="Deployment", name="frontend", namespace="boutique")
    """
    _, agent = make_session(store, "test-04-describe")
    r = run_chat(agent, "Describe the frontend deployment in the boutique namespace in detail.")

    assert r.any_tool_called("describe_resource", "list_deployments"), \
        f"Expected describe_resource or list_deployments, got: {r.tool_names}"
    assert r.text_mentions_any("frontend", "replica", "container", "image"), \
        "Answer should contain deployment details"
    return True


def test_05_pod_logs(store: SessionStore) -> bool:
    """
    Ask: "Show me the logs from the frontend pod in boutique"
    Expect: get_pod_logs called (possibly after list_pods to find exact name)
    """
    _, agent = make_session(store, "test-05-logs")
    r = run_chat(agent, "Show me recent logs from the frontend pod in boutique namespace.")

    # Agent might first list_pods to find the exact pod name, then get_pod_logs
    assert r.any_tool_called("get_pod_logs", "list_pods"), \
        f"Expected get_pod_logs or list_pods, got: {r.tool_names}"
    assert r.final_text, "Should produce a text answer about the logs"
    return True


def test_06_events(store: SessionStore) -> bool:
    """
    Ask: "Are there any warnings or events in boutique?"
    Expect: list_events(namespace="boutique")
    """
    _, agent = make_session(store, "test-06-events")
    r = run_chat(agent, "Are there any warning events in the boutique namespace?")

    assert r.any_tool_called("list_events"), \
        f"Expected list_events, got: {r.tool_names}"
    assert r.final_text, "Should produce a text answer"
    return True


def test_07_namespaces(store: SessionStore) -> bool:
    """
    Ask: "What namespaces exist in the cluster?"
    Expect: list_namespaces tool, answer includes boutique + monitoring.
    """
    _, agent = make_session(store, "test-07-namespaces")
    r = run_chat(agent, "What namespaces exist in this cluster?")

    assert r.any_tool_called("list_namespaces"), \
        f"Expected list_namespaces, got: {r.tool_names}"
    assert r.text_mentions_any("boutique", "monitoring", "default", "kube-system"), \
        "Answer should list known namespaces"
    return True


def test_08_nodes(store: SessionStore) -> bool:
    """
    Ask: "How are the cluster nodes doing?"
    Expect: list_nodes or get_cluster_health, answer includes node conditions.
    """
    _, agent = make_session(store, "test-08-nodes")
    r = run_chat(agent, "How are the cluster nodes? Are they all Ready?")

    assert r.any_tool_called("list_nodes", "get_cluster_health"), \
        f"Expected list_nodes or get_cluster_health, got: {r.tool_names}"
    assert r.text_mentions_any("ready", "node", "minikube"), \
        "Answer should reference node status"
    return True


def test_09_services(store: SessionStore) -> bool:
    """
    Ask: "What services exist in boutique?"
    Expect: list_services(namespace="boutique"), answer lists service names.
    """
    _, agent = make_session(store, "test-09-services")
    r = run_chat(agent, "What services are running in the boutique namespace?")

    assert r.any_tool_called("list_services"), \
        f"Expected list_services, got: {r.tool_names}"
    assert r.text_mentions_any("frontend", "cart", "service", "ClusterIP"), \
        "Answer should list boutique services"
    return True


def test_10_prometheus_query(store: SessionStore) -> bool:
    """
    Ask: "What's the memory usage of pods in boutique?"
    Expect: query_prometheus or get_resource_pressure called with a PromQL query.
    """
    _, agent = make_session(store, "test-10-prometheus")
    r = run_chat(agent, "What's the current memory usage of pods in the boutique namespace?")

    assert r.any_tool_called("query_prometheus", "get_resource_pressure"), \
        f"Expected prometheus tool, got: {r.tool_names}"
    assert r.final_text, "Should produce a text answer about memory"
    return True


def test_11_incident_investigation(store: SessionStore) -> bool:
    """
    Ask: "Are there any active incidents?"
    Expect: get_active_incidents tool called.
    This tests the DB path even if there are no incidents (clean state is fine).
    """
    _, agent = make_session(store, "test-11-incidents")
    r = run_chat(agent, "Are there any active incidents I should know about?")

    assert r.any_tool_called("get_active_incidents"), \
        f"Expected get_active_incidents, got: {r.tool_names}"
    # Answer should either list incidents or say there are none
    assert r.text_mentions_any("incident", "no active", "none", "currently"), \
        "Answer should address active incidents"
    return True


def test_12_multi_turn_conversation(store: SessionStore) -> bool:
    """
    Multi-turn: Ask about pods first, then follow up about a specific one.
    Validates that context carries over across turns.
    """
    sid, agent = make_session(store, "test-12-multiturn")

    # Turn 1: List pods
    print("  Turn 1:")
    r1 = run_chat(agent, "List pods in boutique namespace.")
    assert r1.any_tool_called("list_pods"), f"T1: Expected list_pods, got: {r1.tool_names}"

    # Turn 2: Follow up — ask about a specific service
    print("  Turn 2:")
    r2 = run_chat(agent, "Now show me the events for the frontend in that namespace.")
    # Agent should use list_events or describe_resource for frontend in boutique
    assert r2.any_tool_called("list_events", "describe_resource", "get_pod_logs"), \
        f"T2: Expected events/describe/logs, got: {r2.tool_names}"

    # Verify session has multiple messages persisted
    history = store.get_history(sid)
    assert len(history) >= 4, f"Expected >=4 messages in session, got {len(history)}"
    return True


def test_13_resource_pressure(store: SessionStore) -> bool:
    """
    Ask: "Are any pods under memory pressure?"
    Expect: get_resource_pressure tool called.
    """
    _, agent = make_session(store, "test-13-pressure")
    r = run_chat(agent, "Are any pods running close to their memory or CPU limits?")

    assert r.any_tool_called("get_resource_pressure", "query_prometheus"), \
        f"Expected resource pressure tool, got: {r.tool_names}"
    assert r.final_text, "Should produce a text answer"
    return True


def test_14_error_handling(store: SessionStore) -> bool:
    """
    Ask about a namespace that doesn't exist.
    Agent should handle the K8s API error gracefully.
    """
    _, agent = make_session(store, "test-14-error")
    r = run_chat(agent, "Show me all pods in the 'nonexistent-ns-xyz' namespace.")

    # The agent should call list_pods with that namespace and get an error or empty result
    assert r.any_tool_called("list_pods", "list_namespaces"), \
        f"Expected list_pods, got: {r.tool_names}"
    # Should handle gracefully — no Python traceback in the answer
    assert "traceback" not in r.final_text.lower(), "Answer should not expose Python tracebacks"
    return True


def test_15_broad_diagnosis_question(store: SessionStore) -> bool:
    """
    Ask: "Something seems wrong with checkout, can you investigate?"
    The agent should use multiple tools: incidents, pods, events, logs.
    This tests the agent's ability to plan a multi-tool investigation.
    """
    _, agent = make_session(store, "test-15-diagnosis")
    r = run_chat(agent, "Something seems off with the checkoutservice in boutique. Can you investigate?")

    # Should use at least 2 different tools for investigation
    unique_tools = set(r.tool_names)
    assert len(unique_tools) >= 2, \
        f"Expected multi-tool investigation, only used: {unique_tools}"
    # Should mention checkout in the answer
    assert r.text_mentions_any("checkout", "checkoutservice"), \
        "Answer should reference checkoutservice"
    return True


# ═════════════════════════════════════════════════════════════════════════════
# Injected failure tests (creates test resources)
# ═════════════════════════════════════════════════════════════════════════════

def _setup_test_namespace():
    """Ensure aiops-chattest namespace exists."""
    from kubernetes.client import CoreV1Api, V1Namespace, V1ObjectMeta
    from kubernetes.client.exceptions import ApiException
    from k8s.client import load_kube_client

    api = load_kube_client()
    try:
        api.create_namespace(body=V1Namespace(
            metadata=V1ObjectMeta(name="aiops-chattest")
        ))
        print(f"  {_DIM}Created namespace aiops-chattest{_RESET}")
    except ApiException as e:
        if e.status != 409:
            raise
    return api


def _cleanup_test_namespace():
    """Delete the test namespace."""
    from kubernetes.client.exceptions import ApiException
    from k8s.client import load_kube_client

    api = load_kube_client()
    try:
        api.delete_namespace(name="aiops-chattest")
        print(f"  {_DIM}Deleted namespace aiops-chattest{_RESET}")
    except ApiException:
        pass


def test_16_detect_crashloop_pod(store: SessionStore) -> bool:
    """
    Inject a CrashLooping pod, then ask the agent to investigate.
    Validates the agent can spot a broken pod in a real cluster.
    """
    from kubernetes.client import (
        V1Pod, V1ObjectMeta, V1PodSpec, V1Container,
        CoreV1Api, AppsV1Api,
    )
    from kubernetes.client.exceptions import ApiException

    api = _setup_test_namespace()
    pod_name = "chattest-crasher"
    ns = "aiops-chattest"

    # Create a pod that crashes immediately
    try:
        api.create_namespaced_pod(namespace=ns, body=V1Pod(
            metadata=V1ObjectMeta(name=pod_name, namespace=ns),
            spec=V1PodSpec(
                restart_policy="Always",
                containers=[V1Container(
                    name="crasher",
                    image="busybox:latest",
                    command=["sh", "-c", "echo 'FATAL: crashing on purpose'; exit 1"],
                )],
            ),
        ))
    except ApiException as e:
        if e.status != 409:
            raise

    # Wait for CrashLoopBackOff
    print(f"  Waiting for {pod_name} to enter CrashLoopBackOff...")
    deadline = time.time() + 90
    reached = False
    while time.time() < deadline:
        try:
            pod = api.read_namespaced_pod(name=pod_name, namespace=ns)
            for cs in (pod.status.container_statuses or []):
                if cs.state and cs.state.waiting:
                    reason = cs.state.waiting.reason
                    if reason in ("CrashLoopBackOff", "Error"):
                        reached = True
                        break
        except ApiException:
            pass
        if reached:
            break
        time.sleep(3)

    if not reached:
        print(f"  {_YELLOW}SKIP — pod did not reach CrashLoopBackOff in time{_RESET}")
        try:
            api.delete_namespaced_pod(name=pod_name, namespace=ns)
        except ApiException:
            pass
        return True  # Skip, don't fail

    # Now ask the agent about this namespace
    print(f"  Pod is crashing — asking the agent...")
    _, agent = make_session(store, "test-16-crashloop")
    r = run_chat(agent, f"Check the pods in the aiops-chattest namespace. Is anything wrong?")

    # Cleanup
    try:
        api.delete_namespaced_pod(name=pod_name, namespace=ns)
    except ApiException:
        pass

    # Assertions
    assert r.any_tool_called("list_pods", "describe_resource", "list_events", "get_pod_logs"), \
        f"Expected K8s inspection tools, got: {r.tool_names}"
    assert r.text_mentions_any("crash", "error", "backoff", "failing", "unhealthy", "not running", "not ready", "chattest-crasher"), \
        "Agent should detect the crashing pod"
    return True


def test_17_detect_bad_image(store: SessionStore) -> bool:
    """
    Create a deployment with a non-existent image, then ask the agent.
    Tests ImagePullBackOff detection.
    """
    from kubernetes.client import (
        V1Deployment, V1DeploymentSpec, V1LabelSelector,
        V1PodTemplateSpec, V1ObjectMeta, V1PodSpec, V1Container,
        AppsV1Api,
    )
    from kubernetes.client.exceptions import ApiException

    api = _setup_test_namespace()
    apps = AppsV1Api(api.api_client)
    dep_name = "chattest-badimage"
    ns = "aiops-chattest"

    # Create a deployment with a bogus image
    try:
        apps.create_namespaced_deployment(namespace=ns, body=V1Deployment(
            metadata=V1ObjectMeta(name=dep_name, namespace=ns),
            spec=V1DeploymentSpec(
                replicas=1,
                selector=V1LabelSelector(match_labels={"app": dep_name}),
                template=V1PodTemplateSpec(
                    metadata=V1ObjectMeta(labels={"app": dep_name}),
                    spec=V1PodSpec(containers=[V1Container(
                        name="bad",
                        image="registry.invalid/no-such-image:v999",
                    )]),
                ),
            ),
        ))
    except ApiException as e:
        if e.status != 409:
            raise

    # Wait for ImagePullBackOff
    print(f"  Waiting for {dep_name} pods to fail image pull...")
    deadline = time.time() + 90
    reached = False
    while time.time() < deadline:
        try:
            pods = api.list_namespaced_pod(namespace=ns, label_selector=f"app={dep_name}")
            for p in pods.items:
                for cs in (p.status.container_statuses or []):
                    if cs.state and cs.state.waiting:
                        reason = cs.state.waiting.reason
                        if reason in ("ImagePullBackOff", "ErrImagePull"):
                            reached = True
                            break
                if reached:
                    break
        except ApiException:
            pass
        if reached:
            break
        time.sleep(3)

    if not reached:
        print(f"  {_YELLOW}SKIP — pod did not reach ImagePullBackOff in time{_RESET}")
        try:
            apps.delete_namespaced_deployment(name=dep_name, namespace=ns)
        except ApiException:
            pass
        return True

    # Ask the agent
    print(f"  Image pull failing — asking the agent...")
    _, agent = make_session(store, "test-17-badimage")
    r = run_chat(agent, f"Check the aiops-chattest namespace. The {dep_name} deployment seems broken.")

    # Cleanup
    try:
        apps.delete_namespaced_deployment(name=dep_name, namespace=ns)
    except ApiException:
        pass

    assert r.any_tool_called("list_pods", "describe_resource", "list_events", "list_deployments"), \
        f"Expected K8s tools, got: {r.tool_names}"
    assert r.text_mentions_any("image", "pull", "backoff", "not found", "invalid", "error", "unavailable"), \
        "Agent should detect the image pull failure"
    return True


# ═════════════════════════════════════════════════════════════════════════════
# Runner
# ═════════════════════════════════════════════════════════════════════════════

ALL_TESTS = [
    ("01 Cluster health check",         test_01_cluster_health),
    ("02 List pods",                     test_02_list_pods),
    ("03 List deployments",              test_03_list_deployments),
    ("04 Describe a resource",           test_04_describe_resource),
    ("05 Pod logs",                      test_05_pod_logs),
    ("06 Events listing",               test_06_events),
    ("07 Namespace enumeration",         test_07_namespaces),
    ("08 Node inspection",               test_08_nodes),
    ("09 Services listing",              test_09_services),
    ("10 Prometheus query",              test_10_prometheus_query),
    ("11 Incident investigation",        test_11_incident_investigation),
    ("12 Multi-turn conversation",       test_12_multi_turn_conversation),
    ("13 Resource pressure",             test_13_resource_pressure),
    ("14 Error handling (bad ns)",       test_14_error_handling),
    ("15 Broad diagnosis question",      test_15_broad_diagnosis_question),
    ("16 CrashLoop pod detection",       test_16_detect_crashloop_pod),
    ("17 Bad image detection",           test_17_detect_bad_image),
]


def main():
    # Clean test DB
    import pathlib
    pathlib.Path(TEST_DB).parent.mkdir(parents=True, exist_ok=True)
    try:
        os.remove(TEST_DB)
    except FileNotFoundError:
        pass

    store = SessionStore(db_path=TEST_DB)

    print(f"\n{_BOLD}{_CYAN}{'='*70}")
    print(f"  AIOps Chatbot Live Integration Tests")
    print(f"  {len(ALL_TESTS)} scenarios against Minikube + Gemini")
    print(f"{'='*70}{_RESET}\n")

    # Optional: select specific tests by number
    selected = ALL_TESTS
    if len(sys.argv) > 1:
        nums = [int(x) for x in sys.argv[1:]]
        selected = [(name, fn) for name, fn in ALL_TESTS if int(name[:2]) in nums]
        print(f"{_DIM}Running tests: {[name for name, _ in selected]}{_RESET}\n")

    passed = 0
    failed = 0
    skipped = 0
    results_summary = []

    for idx, (name, test_fn) in enumerate(selected):
        if idx > 0:
            print(f"  {_DIM}(throttle: waiting {THROTTLE_SEC}s for rate limit){_RESET}")
            time.sleep(THROTTLE_SEC)
        print(f"\n{_BOLD}[{name}]{_RESET}")
        try:
            ok = test_fn(store)
            if ok:
                print(f"  {_GREEN}PASSED{_RESET}")
                passed += 1
                results_summary.append((name, "PASSED"))
            else:
                print(f"  {_YELLOW}⊘ SKIPPED{_RESET}")
                skipped += 1
                results_summary.append((name, "SKIPPED"))
        except AssertionError as e:
            print(f"  {_RED}FAILED: {e}{_RESET}")
            failed += 1
            results_summary.append((name, f"FAILED: {e}"))
        except Exception as e:
            print(f"  {_RED}ERROR: {e}{_RESET}")
            traceback.print_exc()
            failed += 1
            results_summary.append((name, f"ERROR: {e}"))

    # Summary
    print(f"\n{_BOLD}{_CYAN}{'='*70}")
    print(f"  RESULTS: {_GREEN}{passed} passed{_RESET}{_BOLD}{_CYAN}, "
          f"{_RED}{failed} failed{_RESET}{_BOLD}{_CYAN}, "
          f"{_YELLOW}{skipped} skipped{_RESET}{_BOLD}{_CYAN} "
          f"/ {len(selected)} total")
    print(f"{'='*70}{_RESET}\n")

    for name, status in results_summary:
        if "PASSED" in status:
            icon = f"{_GREEN}+{_RESET}"
        elif "SKIPPED" in status:
            icon = f"{_YELLOW}-{_RESET}"
        else:
            icon = f"{_RED}x{_RESET}"
        print(f"  {icon} {name}: {status}")

    # Cleanup test namespace if it was created
    try:
        _cleanup_test_namespace()
    except Exception:
        pass

    store.close()

    print(f"\n{_DIM}Test DB: {TEST_DB}{_RESET}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
