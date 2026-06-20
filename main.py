from __future__ import annotations

import atexit
import threading
import time
import traceback
from datetime import datetime

from k8s.client import load_kube_client
from triggers.event_watcher import EventWatcher
from triggers.pod_state_watcher import PodStateWatcher
from triggers.endpoint_watcher import EndpointWatcher
from triggers.prometheus_watcher import PrometheusWatcher
from analyzers.base import Finding, Severity
from snapshot.fetcher import SnapshotFetcher
from engine.finding_store import FindingStore
from engine.namespace_aggregator import NamespaceAggregator
from engine.incident_store import IncidentStore
from engine.correlation_engine import CorrelationEngine
from analyzers.registry import AnalyzerRegistry

from analyzers.pod.crashloop           import PodCrashLoopAnalyzer
from analyzers.pod.image_pull          import PodImagePullAnalyzer
from analyzers.pod.pending             import PodPendingAnalyzer
from analyzers.deployment.unavailable  import DeploymentUnavailableAnalyzer
from analyzers.node.conditions         import NodeConditionAnalyzer
from analyzers.service.no_endpoints    import ServiceEndpointMissingAnalyzer
from analyzers.storage.pvc             import PVCAnalyzer
from analyzers.storage.volume_mount    import VolumeMountAnalyzer
from analyzers.scheduler.scheduling    import SchedulerFailureAnalyzer
from analyzers.statefulset.unavailable import StatefulSetUnavailableAnalyzer
from analyzers.daemonset.unavailable   import DaemonSetUnavailableAnalyzer
from analyzers.job.failed              import JobFailureAnalyzer


class AIOpsAgent:
    # ReplicaSet Warning events are resolved to their parent Deployment.
    _KIND_ALIAS: dict[str, str] = {"ReplicaSet": "Deployment"}
    _SNAPSHOT_TTL: float = 15.0  # seconds

    def __init__(self) -> None:
        self.api      = load_kube_client()
        self.fetcher  = SnapshotFetcher(self.api)
        self.store    = FindingStore()
        self.namespace_aggregator = NamespaceAggregator(window_seconds=60, min_threshold=3)
        self.incident_store     = IncidentStore()
        self.correlation_engine = CorrelationEngine(self.incident_store, self.store)
        self.last_auto_resolve    = datetime.utcnow()
        self._notified_incidents: set[str] = set()
        self._snapshot_cache: dict[tuple, tuple[float, dict]] = {}
        self._state_lock = threading.Lock()

        self.registry = AnalyzerRegistry()
        for analyzer in [
            PodCrashLoopAnalyzer(),
            PodImagePullAnalyzer(),
            PodPendingAnalyzer(),
            VolumeMountAnalyzer(),
            SchedulerFailureAnalyzer(),
            DeploymentUnavailableAnalyzer(),
            NodeConditionAnalyzer(),
            ServiceEndpointMissingAnalyzer(),
            PVCAnalyzer(),
            StatefulSetUnavailableAnalyzer(),
            DaemonSetUnavailableAnalyzer(),
            JobFailureAnalyzer(),
        ]:
            self.registry.register(analyzer)

        kinds = sorted({k for a in self.registry.analyzers for k in a.supported_kinds})
        print(f"[AIOps] {len(self.registry.analyzers)} analyzers ready for kinds: {kinds}")

    # Hooks — no-ops here; override in subclasses to observe pipeline stages.

    def on_k8s_event(self, kind: str, namespace: str, name: str,
                     reason: str, message: str) -> None: ...

    def on_finding(self, finding) -> None: ...

    def on_correlation(self, suppressed_count: int) -> None: ...

    def on_incident(self, incident_id: str, scope: str,
                    resource: str, title: str) -> None: ...

    # Pipeline

    def handle_warning(self, kind: str, namespace: str, name: str,
                       uid: str, reason: str, message: str) -> None:
        self.on_k8s_event(kind, namespace, name, reason, message)

        try:
            uid_display = (uid[:8] + "...") if uid and uid != "unknown" else "unknown"
            print(f"\n[Trigger] {kind}/{namespace}/{name} (uid={uid_display}) — {reason}")

            snapshot = self._fetch_snapshot(kind, namespace, name)
            if not snapshot or not isinstance(snapshot, dict) or "metadata" not in snapshot:
                return

            findings  = self._run_analyzers(kind, snapshot)
            findings += self.namespace_aggregator.aggregate(findings)

            for f in findings:
                self.on_finding(f)

            enriched     = self._enrich_findings(findings, snapshot, uid)
            new_findings = self.store.filter_new(enriched)

            if new_findings:
                suppressed     = self.correlation_engine.correlate(self._to_dicts(new_findings))
                suppressed_ids = {f["id"] for f in suppressed}

                if suppressed:
                    self.on_correlation(len(suppressed))

                self._check_new_incidents()

                for f in new_findings:
                    if f.id in suppressed_ids:
                        print(f"[Correlation] Suppressed: {f.kind}/{f.resource} ({f.reason})")
                    else:
                        self._emit_finding(f)

            now = datetime.utcnow()
            if (now - self.last_auto_resolve).total_seconds() > 300:
                resolved = self.correlation_engine.auto_resolve_stale_incidents()
                if resolved:
                    print(f"[Incidents] Auto-resolved {resolved} stale incidents")
                self.last_auto_resolve = now

        except Exception as e:
            print(f"[AIOps] ERROR in handle_warning: {e}")
            print(traceback.format_exc())

    def _check_new_incidents(self) -> None:
        try:
            for incident in self.incident_store.list_active_incidents():
                with self._state_lock:
                    already_seen = incident.id in self._notified_incidents
                    if not already_seen:
                        self._notified_incidents.add(incident.id)
                if not already_seen:
                    self.on_incident(
                        incident.id, incident.scope,
                        incident.resource, incident.title,
                    )
        except Exception as e:
            print(f"[AIOps] ERROR checking new incidents: {e}")

    def _run_analyzers(self, kind: str, snapshot: dict) -> list:
        dispatch_kind = self._KIND_ALIAS.get(kind, kind)
        findings      = []
        known_kinds   = {k for a in self.registry.analyzers for k in a.supported_kinds}

        if dispatch_kind not in known_kinds:
            print(f"[AIOps] No analyzers registered for kind: {dispatch_kind}")
            return findings

        for analyzer in self.registry.analyzers:
            if dispatch_kind in analyzer.supported_kinds:
                findings.extend(analyzer.analyze(snapshot))

        return findings

    def _to_dicts(self, findings: list) -> list[dict]:
        result = []
        for f in findings:
            rc = dict(f.raw_context)
            rc["owner_chain"] = ",".join(f.owner_chain) if f.owner_chain else ""
            result.append({
                "id":           f.id,
                "scope":        f.kind,
                "resource":     f.resource,
                "resource_uid": f.resource_uid,
                "severity":     f.severity.name,
                "reason":       f.reason,
                "message":      f.message,
                "raw_context":  rc,
            })
        return result

    def _fetch_snapshot(self, kind: str, namespace: str, name: str) -> dict | None:
        cache_key = (kind, namespace, name)
        now = time.monotonic()
        with self._state_lock:
            cached = self._snapshot_cache.get(cache_key)
        if cached:
            fetched_at, snapshot = cached
            if now - fetched_at < self._SNAPSHOT_TTL:
                return snapshot

        fetchers = {
            "Pod":                   lambda: self.fetcher.fetch_pod_snapshot(namespace, name),
            "Deployment":            lambda: self.fetcher.fetch_deployment_snapshot(namespace, name),
            "ReplicaSet":            lambda: self.fetcher.fetch_replicaset_parent_deployment(namespace, name),
            "Service":               lambda: self.fetcher.fetch_service_snapshot(namespace, name),
            "Node":                  lambda: self.fetcher.fetch_node_snapshot(name),
            "PersistentVolumeClaim": lambda: self.fetcher.fetch_pvc_snapshot(namespace, name),
            "StatefulSet":           lambda: self.fetcher.fetch_statefulset_snapshot(namespace, name),
            "DaemonSet":             lambda: self.fetcher.fetch_daemonset_snapshot(namespace, name),
            "Job":                   lambda: self.fetcher.fetch_job_snapshot(namespace, name),
        }
        fetch = fetchers.get(kind)
        if not fetch:
            print(f"[AIOps] Unsupported kind: {kind}")
            return None

        result = fetch()
        if result:
            with self._state_lock:
                self._snapshot_cache[cache_key] = (now, result)
        return result

    def _enrich_findings(self, findings: list, snapshot: dict, event_uid: str) -> list:
        from dataclasses import replace

        metadata     = snapshot.get("metadata") or {}
        snap_uid     = metadata.get("uid", event_uid)
        owner_chain  = snapshot.get("owner_chain", [])
        timestamp    = datetime.utcnow().isoformat()
        raw_logs     = snapshot.get("logs") or ""
        log_snippet  = "\n".join(raw_logs.splitlines()[-50:]) if raw_logs else ""
        node_name    = (snapshot.get("spec") or {}).get("node_name") or ""

        enriched = []
        for f in findings:
            uid = f.resource_uid if (f.resource_uid and f.resource_uid not in ("", "unknown")) else snap_uid
            ctx = {**f.raw_context, "node_name": node_name} if node_name else dict(f.raw_context)

            enriched.append(replace(
                f,
                resource_uid=uid,
                owner_chain=owner_chain,
                timestamp=timestamp,
                log_snippet=log_snippet,
                raw_context=ctx,
            ))
        return enriched

    def _emit_finding(self, finding) -> None:
        owner_info = f" (owner: {finding.owner_chain[-1]})" if finding.owner_chain else ""
        print(f"\n[Finding] {finding.severity.name} | {finding.resource}{owner_info}")
        print(f"  Analyzer: {finding.analyzer}")
        print(f"  Reason:   {finding.reason}")
        print(f"  Message:  {finding.message}")

    def handle_direct_finding(
        self,
        kind: str,
        namespace: str,
        name: str,
        reason: str,
        message: str,
        extra_context: dict | None = None,
    ) -> None:
        """Accept a pre-built metric finding (e.g. from Prometheus) and run it through dedup/correlation."""
        from dataclasses import replace as dc_replace

        resource = f"{namespace}/{name}"
        finding = Finding(
            analyzer="PrometheusWatcher",
            resource=resource,
            kind=kind,
            severity=Severity.WARNING,
            reason=reason,
            message=message,
            raw_context=extra_context or {},
        )

        # Best-effort: enrich with K8s uid + owner_chain from a live snapshot
        try:
            snapshot = self._fetch_snapshot(kind, namespace, name)
            if snapshot:
                metadata = snapshot.get("metadata") or {}
                uid = metadata.get("uid", "unknown")
                owner_chain = snapshot.get("owner_chain", [])
                node_name = (snapshot.get("spec") or {}).get("node_name", "")
                ctx = {**finding.raw_context, "node_name": node_name} if node_name else dict(finding.raw_context)
                finding = dc_replace(
                    finding,
                    resource_uid=uid,
                    owner_chain=owner_chain,
                    timestamp=datetime.utcnow().isoformat(),
                    raw_context=ctx,
                )
        except Exception as e:
            print(f"[AIOps] Enrichment failed for {kind}/{namespace}/{name}: {e}")

        try:
            new_findings = self.store.filter_new([finding])
            if new_findings:
                suppressed = self.correlation_engine.correlate(self._to_dicts(new_findings))
                suppressed_ids = {f["id"] for f in suppressed}
                if suppressed:
                    self.on_correlation(len(suppressed))
                self._check_new_incidents()
                for f in new_findings:
                    if f.id not in suppressed_ids:
                        self._emit_finding(f)
        except Exception as e:
            print(f"[AIOps] ERROR in handle_direct_finding: {e}")
            print(traceback.format_exc())

    def run(self) -> None:
        print("[AIOps] Starting watchers...")

        pod_watcher  = PodStateWatcher(self.api, self.handle_warning)
        ep_watcher   = EndpointWatcher(self.api, self.handle_warning)
        prom_watcher = PrometheusWatcher(
            handle_warning=self.handle_warning,
            handle_direct_finding=self.handle_direct_finding,
            poll_interval=30,
        )

        for watcher, name in [
            (pod_watcher, "pod-state-watcher"),
            (ep_watcher, "endpoint-watcher"),
            (prom_watcher, "prometheus-watcher"),
        ]:
            t = threading.Thread(target=watcher.run, daemon=True, name=name)
            t.start()
            print(f"[AIOps] Started background thread: {name}")

        print("[AIOps] Starting event watcher (main thread)...")
        EventWatcher(self.api, self.handle_warning).run()


if __name__ == "__main__":
    agent = AIOpsAgent()
    atexit.register(agent.store.close)
    atexit.register(agent.incident_store.close)
    agent.run()

