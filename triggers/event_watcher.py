"""
Event-driven trigger system.
Watches Kubernetes Events API for Warning events, then delegates to handler.
"""

from kubernetes import watch
from kubernetes.client import CoreV1Api
from kubernetes.client.exceptions import ApiException
import time


class EventWatcher:
    """
    Watches Kubernetes Events API and triggers on Warning events.
    This is the PRIMARY trigger mechanism (DESIGN.md Section 3).
    """

    def __init__(self, api: CoreV1Api, on_warning_callback):
        """
        Args:
            api: Kubernetes CoreV1Api client
            on_warning_callback: Function(kind, namespace, name, reason, message)
        """
        self.api = api
        self.on_warning = on_warning_callback
        self.resource_version = None

    def run(self):
        """
        Run continuous event watch loop with auto-recovery.
        Listens for Warning events and triggers handler.
        """
        # Seed resourceVersion from a live list so we only see events from
        # this moment forward — never replay historical / deleted-resource events.
        self.resource_version = self._current_resource_version()
        while True:
            try:
                self._watch_events()
            except ApiException as e:
                if e.status == 410:  # ResourceVersion too old / expired
                    print(f"[EventWatcher] Resource version expired, restarting watch")
                    # Get a fresh resourceVersion so we resume from NOW, not history
                    self.resource_version = self._current_resource_version()
                else:
                    print(f"[EventWatcher] API error: {e}, retrying in 5s")
                time.sleep(5)
            except Exception as e:
                print(f"[EventWatcher] Unexpected error: {e}, retrying in 5s")
                time.sleep(5)

    def _current_resource_version(self) -> str:
        """
        List events with limit=1 to get the cluster's current resourceVersion.
        Watching from this version means we only receive events that happen
        AFTER this call — no historical replay.

        Retries up to 3 times on failure so we never fall back to an empty
        resourceVersion (which would replay ALL historical events).
        """
        for attempt in range(3):
            try:
                result = self.api.list_event_for_all_namespaces(limit=1)
                return result.metadata.resource_version
            except Exception as e:
                print(f"[EventWatcher] Failed to seed resourceVersion (attempt {attempt + 1}/3): {e}")
                time.sleep(2)
        # Last resort: returning "0" starts the watch from the oldest
        # available event in etcd, which is better than "" (full replay).
        print("[EventWatcher] WARNING: Could not seed resourceVersion, using '0'")
        return "0"  

    def _watch_events(self):
        """
        Watch events stream and filter for Warning type.
        """
        w = watch.Watch()

        for event in w.stream(
            self.api.list_event_for_all_namespaces,
            resource_version=self.resource_version,
            timeout_seconds=300,  # 5 min timeout, then reconnect
        ):
            evt = event["object"]
            event_type = event["type"]

            # Update resource version for resume capability
            rv = evt.metadata.resource_version
            if rv:
                self.resource_version = rv

            # Only process Warning events (not Normal)
            if evt.type == "Warning" and event_type in ("ADDED", "MODIFIED"):
                self._handle_warning(evt)

    def _handle_warning(self, evt):
        """
        Delegate to user-provided callback when Warning event detected.
        """
        obj = evt.involved_object
        
        # Guard against missing fields in event object
        if not obj or not obj.kind or not obj.name:
            return
        
        # UID might be None for some events (e.g., namespace-scoped, old events)
        uid = getattr(obj, 'uid', None) or 'unknown'
        namespace = getattr(obj, 'namespace', None) or 'default'

        self.on_warning(
            kind=obj.kind,
            namespace=namespace,
            name=obj.name,
            uid=uid,
            reason=evt.reason or 'Unknown',
            message=evt.message or '',
        )
