from __future__ import annotations

import time

from kubernetes import watch
from kubernetes.client import CoreV1Api
from kubernetes.client.exceptions import ApiException


class EndpointWatcher:
    """
    Watches Endpoints objects for services losing all their ready backends.
    Fires handle_warning(kind="Service") when endpoint count drops to 0.
    Catches the cascade case: dependency deleted → service silently loses all pods.
    """

    def __init__(self, api: CoreV1Api, on_warning_callback, namespace: str | None = None):
        self.api = api
        self.on_warning = on_warning_callback
        self.namespace = namespace
        self._last_count: dict[str, int] = {}  # "namespace/name" -> last known count

    def run(self) -> None:
        while True:
            try:
                self._watch()
            except ApiException as e:
                if e.status != 410:
                    print(f"[EndpointWatcher] API error: {e}, retrying in 5s")
                    time.sleep(5)
            except Exception as e:
                print(f"[EndpointWatcher] Error: {e}, retrying in 5s")
                time.sleep(5)

    def _watch(self) -> None:
        w = watch.Watch()
        if self.namespace:
            stream = w.stream(
                self.api.list_namespaced_endpoints,
                self.namespace,
                timeout_seconds=300,
            )
        else:
            stream = w.stream(
                self.api.list_endpoints_for_all_namespaces,
                timeout_seconds=300,
            )

        for event in stream:
            if event["type"] not in ("ADDED", "MODIFIED"):
                continue
            self._check_endpoints(event["object"])

    def _check_endpoints(self, ep) -> None:
        meta = ep.metadata
        if not meta:
            return

        namespace = meta.namespace or "default"
        name = meta.name
        key = f"{namespace}/{name}"

        subsets = ep.subsets or []
        count = sum(len(s.addresses or []) for s in subsets)

        # Use current count as default so first-seen event doesn't fire
        prev = self._last_count.get(key, count)
        self._last_count[key] = count

        # Only fire on transition: had backends → now zero
        if count == 0 and prev > 0:
            self.on_warning(
                kind="Service",
                namespace=namespace,
                name=name,
                uid="unknown",
                reason="NoEndpoints",
                message=(
                    f"Service '{name}' in '{namespace}' lost all endpoints "
                    f"(dropped from {prev} to 0)."
                ),
            )
