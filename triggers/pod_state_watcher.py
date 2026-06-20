from __future__ import annotations

import time

from kubernetes import watch
from kubernetes.client import CoreV1Api
from kubernetes.client.exceptions import ApiException


_ERRORING_REASONS = frozenset({
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
    "ErrImageNeverPull", "OOMKilled", "Error", "ContainerCannotRun",
})


class PodStateWatcher:
    """
    Watches pod MODIFIED events via the K8s watch API.
    Fires handle_warning when a pod transitions from Ready to NotReady.
    Complements the EventWatcher — catches silent probe failures with no Warning event.
    """

    def __init__(self, api: CoreV1Api, on_warning_callback, namespace: str | None = None):
        self.api = api
        self.on_warning = on_warning_callback
        self.namespace = namespace
        self._last_ready: dict[str, bool | None] = {}  # pod uid -> last known ready state

    def run(self) -> None:
        while True:
            try:
                self._watch()
            except ApiException as e:
                if e.status != 410:
                    print(f"[PodStateWatcher] API error: {e}, retrying in 5s")
                    time.sleep(5)
            except Exception as e:
                print(f"[PodStateWatcher] Error: {e}, retrying in 5s")
                time.sleep(5)

    def _watch(self) -> None:
        w = watch.Watch()
        if self.namespace:
            stream = w.stream(
                self.api.list_namespaced_pod,
                self.namespace,
                timeout_seconds=300,
            )
        else:
            stream = w.stream(
                self.api.list_pod_for_all_namespaces,
                timeout_seconds=300,
            )

        for event in stream:
            if event["type"] not in ("ADDED", "MODIFIED"):
                continue
            self._check_pod(event["object"])

    def _check_pod(self, pod) -> None:
        meta = pod.metadata
        if not meta:
            return

        uid = meta.uid or meta.name
        namespace = meta.namespace or "default"
        name = meta.name
        status = pod.status
        if not status:
            return

        conditions = status.conditions or []
        ready_cond = next((c for c in conditions if c.type == "Ready"), None)
        is_ready = (ready_cond.status == "True") if ready_cond else None

        was_ready = self._last_ready.get(uid)
        self._last_ready[uid] = is_ready

        # Only fire on transition: was ready → now not ready
        if is_ready is False and was_ready is True:
            reason, message = self._get_error_info(status)
            self.on_warning(
                kind="Pod",
                namespace=namespace,
                name=name,
                uid=uid,
                reason=reason,
                message=message,
            )

    def _get_error_info(self, status) -> tuple[str, str]:
        all_statuses = (status.container_statuses or []) + (status.init_container_statuses or [])
        for cs in all_statuses:
            if cs.state:
                if cs.state.waiting and cs.state.waiting.reason in _ERRORING_REASONS:
                    return cs.state.waiting.reason, cs.state.waiting.message or ""
                if cs.state.terminated and cs.state.terminated.reason in _ERRORING_REASONS:
                    return cs.state.terminated.reason, cs.state.terminated.message or ""
        return "NotReady", "Pod transitioned to NotReady state"
