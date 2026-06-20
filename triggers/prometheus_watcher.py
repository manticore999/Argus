from __future__ import annotations

import threading
import time
from typing import Callable

from prometheus_api_client import PrometheusConnect


# Each rule defines a PromQL query and how to route the result into the pipeline.
#
# action "k8s"    → calls handle_warning(kind, namespace, name, ...) so the existing
#                   analyzer chain runs and enriches the finding via a live K8s snapshot.
# action "metric" → calls handle_direct_finding(...) for signals that have no K8s object
#                   counterpart (e.g. memory usage ratio).
#
# name_label / namespace_label: which Prometheus metric labels carry the resource identity.
_RULES: list[dict] = [
    {
        "label": "endpoint_gone",
        "query": (
            'kube_endpoint_address_available{namespace!="monitoring"} == 0'
        ),
        "action": "k8s",
        "kind": "Service",
        "reason": "NoEndpoints",
        "message": lambda m: (
            f"Service '{m.get('endpoint', m.get('service', 'unknown'))}' "
            f"in namespace '{m.get('namespace', '')}' has 0 available endpoints."
        ),
        "name_label": "endpoint",
        "namespace_label": "namespace",
    },
    {
        "label": "deployment_unavailable",
        # Require condition to be sustained for 5m to avoid firing during
        # normal rolling updates where unavailable replicas briefly > 0.
        "query": (
            "min_over_time("
            "kube_deployment_status_replicas_unavailable{namespace!=\"monitoring\"}"
            "[5m]) > 0"
        ),
        "action": "k8s",
        "kind": "Deployment",
        "reason": "UnavailableReplicas",
        "message": lambda m: (
            f"Deployment '{m.get('deployment', 'unknown')}' has had unavailable replicas "
            f"for at least 5 minutes."
        ),
        "name_label": "deployment",
        "namespace_label": "namespace",
    },
    {
        "label": "statefulset_degraded",
        "query": (
            "(kube_statefulset_status_replicas_ready{namespace!=\"monitoring\"}"
            " / kube_statefulset_spec_replicas{namespace!=\"monitoring\"}) < 1"
            " and kube_statefulset_spec_replicas{namespace!=\"monitoring\"} > 0"
        ),
        "action": "k8s",
        "kind": "StatefulSet",
        "reason": "DegradedReplicas",
        "message": lambda m: (
            f"StatefulSet '{m.get('statefulset', 'unknown')}' has replicas not ready."
        ),
        "name_label": "statefulset",
        "namespace_label": "namespace",
    },
    {
        "label": "restart_rate_spike",
        "query": (
            'rate(kube_pod_container_status_restarts_total{namespace!="monitoring"}[5m])'
            " * 300 > 3"
        ),
        "action": "k8s",
        "kind": "Pod",
        "reason": "HighRestartRate",
        "message": lambda m: (
            f"Pod '{m.get('pod', m.get('exported_pod', 'unknown'))}' "
            f"container '{m.get('container', '?')}' is restarting rapidly (>3 restarts/5m)."
        ),
        "name_label": "pod",
        "namespace_label": "namespace",
    },
    {
        "label": "memory_pressure",
        # Threshold at 95% — avoids noise from JVMs that legitimately
        # pre-allocate heap and run at 85-90% continuously.
        # Sustained for 5m window (avg) to filter brief spikes during GC.
        "query": (
            "(avg_over_time(container_memory_working_set_bytes"
            "{namespace!=\"monitoring\",container!=\"\"}[5m])"
            " / on(namespace,pod,container)"
            " kube_pod_container_resource_limits{resource=\"memory\","
            "namespace!=\"monitoring\",container!=\"\"}) > 0.95"
        ),
        "action": "metric",
        "kind": "Pod",
        "reason": "MemoryPressure",
        "message": lambda m: (
            f"Container '{m.get('container', '?')}' in pod '{m.get('pod', '?')}' "
            f"(namespace '{m.get('namespace', '')}') has sustained >95% memory usage "
            f"over 5 minutes — OOM kill likely."
        ),
        "name_label": "pod",
        "namespace_label": "namespace",
        "extra_labels": ["container"],
    },
]


class PrometheusWatcher:
    """
    Periodic PromQL sweep that closes detection gaps not covered by K8s Warning events.
    Fires every poll_interval seconds. Each triggered result routes into the pipeline
    via handle_warning (for K8s-state signals) or handle_direct_finding (for pure metrics).
    Cooldown prevents re-firing the same signal repeatedly.
    """

    COOLDOWN: float = 120.0  # seconds before re-firing the same (rule, resource) pair

    def __init__(
        self,
        handle_warning: Callable,
        handle_direct_finding: Callable,
        prometheus_url: str = "",
        poll_interval: int = 30,
    ):
        from config import PROMETHEUS_URL
        self.handle_warning = handle_warning
        self.handle_direct_finding = handle_direct_finding
        self._url = prometheus_url or PROMETHEUS_URL
        self.prom = PrometheusConnect(url=self._url, disable_ssl=True)
        self.poll_interval = poll_interval
        self._fired: dict[str, float] = {}  # fire_key -> monotonic time last fired

    def run(self) -> None:
        print(f"[Prometheus] Watcher started — polling every {self.poll_interval}s")
        while True:
            try:
                self._poll()
            except Exception as e:
                print(f"[Prometheus] Poll error: {e}")
            time.sleep(self.poll_interval)

    def start_background(self) -> threading.Thread:
        t = threading.Thread(target=self.run, daemon=True, name="prometheus-watcher")
        t.start()
        return t

    def _poll(self) -> None:
        for rule in _RULES:
            try:
                results = self.prom.custom_query(rule["query"])
            except Exception as e:
                print(f"[Prometheus] Query failed ({rule['label']}): {e}")
                continue

            for result in results:
                metric = result.get("metric", {})
                namespace = metric.get(rule["namespace_label"], "default")
                name = metric.get(rule["name_label"], "unknown")

                if not name or name == "unknown":
                    continue

                fire_key = f"{rule['label']}:{namespace}/{name}"
                now = time.monotonic()
                if now - self._fired.get(fire_key, 0) < self.COOLDOWN:
                    continue
                self._fired[fire_key] = now

                message = rule["message"](metric)
                print(f"[Prometheus] {rule['label']} → {rule['kind']} {namespace}/{name}")

                if rule["action"] == "k8s":
                    self.handle_warning(
                        kind=rule["kind"],
                        namespace=namespace,
                        name=name,
                        uid="unknown",
                        reason=rule["reason"],
                        message=message,
                    )
                else:
                    extra = {k: metric.get(k, "") for k in rule.get("extra_labels", [])}
                    self.handle_direct_finding(
                        kind=rule["kind"],
                        namespace=namespace,
                        name=name,
                        reason=rule["reason"],
                        message=message,
                        extra_context=extra,
                    )
