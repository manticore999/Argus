"""
Targeted snapshot fetcher.
Performs on-demand GET operations for specific resources.
Includes owner resolution (Pod → ReplicaSet → Deployment) and bounded log fetching.
"""

from kubernetes.client import CoreV1Api, AppsV1Api, BatchV1Api
from kubernetes.client.exceptions import ApiException


class SnapshotFetcher:
    """
    Fetches snapshots for specific resources on-demand.
    No caching, no background watching - just targeted GET operations.
    Includes owner resolution (Pod → ReplicaSet → Deployment) and bounded logs.
    """

    MAX_OWNER_DEPTH = 3  # Follow owner chain up to 3 levels
    MAX_LOG_LINES = 100  # Last 100 lines per container

    def __init__(self, api: CoreV1Api):
        self.api = api
        self.apps_v1 = AppsV1Api(api.api_client)
        self.batch_v1 = BatchV1Api(api.api_client)

    def fetch_pod_snapshot(self, namespace: str, name: str) -> dict | None:
        """
        Fetch a single Pod and build a snapshot dict for analyzers.
        Returns None only on 404; raises on other API errors.
        """
        try:
            pod = self.api.read_namespaced_pod(name=name, namespace=namespace)
            pod_dict = pod.to_dict()

            if not pod_dict or "metadata" not in pod_dict:
                raise ValueError(f"Invalid pod structure for {namespace}/{name}")

            pod_dict["owner_chain"] = self.resolve_owners(pod_dict)
            pod_dict["logs"] = self._fetch_failing_container_logs(namespace, name, pod_dict)

            return pod_dict

        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def fetch_deployment_snapshot(self, namespace: str, name: str) -> dict | None:
        """
        Fetch a single Deployment and build a snapshot dict for analyzers.
        
        Returns:
            dict: Deployment snapshot (guaranteed valid structure)
            None: Only if not found (404)
        
        Raises:
            Exception: On unexpected API errors (caller should handle)
        """
        try:
            deployment = self.apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
            deployment_dict = deployment.to_dict()
            
            # Ensure minimal required structure exists
            if not deployment_dict or "metadata" not in deployment_dict:
                raise ValueError(f"Invalid deployment structure returned from API for {namespace}/{name}")
            
            # Deployments don't need owner resolution (they're typically top-level)
            # But include empty owner_chain for consistency
            deployment_dict["owner_chain"] = []
            
            return deployment_dict

        except ApiException as e:
            if e.status == 404:
                # Normal: events can arrive after resource deletion
                return None
            else:
                # Unexpected API error - raise so caller can log and handle
                raise

    def fetch_service_snapshot(self, namespace: str, name: str) -> dict | None:
        """
        Fetch a single Service and build a snapshot dict for analyzers.
        Includes endpoint count for endpoint analysis.
        
        Returns:
            dict: Service snapshot with endpoint_addresses count
            None: Only if not found (404)
        
        Raises:
            Exception: On unexpected API errors (caller should handle)
        """
        try:
            service = self.api.read_namespaced_service(name=name, namespace=namespace)
            service_dict = service.to_dict()
            
            # Ensure minimal required structure
            if not service_dict or "metadata" not in service_dict:
                raise ValueError(f"Invalid service structure returned from API for {namespace}/{name}")
            
            # Fetch endpoint information
            try:
                endpoints = self.api.read_namespaced_endpoints(name=name, namespace=namespace)
                # Count total endpoint addresses
                subsets = endpoints.subsets or []
                endpoint_count = sum(len(subset.addresses or []) for subset in subsets)
                service_dict["endpoint_addresses"] = endpoint_count
            except ApiException:
                # Endpoints might not exist yet
                service_dict["endpoint_addresses"] = -1
            
            service_dict["owner_chain"] = []
            
            return service_dict

        except ApiException as e:
            if e.status == 404:
                return None
            else:
                raise

    def fetch_node_snapshot(self, name: str) -> dict | None:
        """
        Fetch a single Node and build a snapshot dict for analyzers.
        
        Returns:
            dict: Node snapshot
            None: Only if not found (404)
        
        Raises:
            Exception: On unexpected API errors (caller should handle)
        """
        try:
            node = self.api.read_node(name=name)
            node_dict = node.to_dict()
            
            # Ensure minimal required structure
            if not node_dict or "metadata" not in node_dict:
                raise ValueError(f"Invalid node structure returned from API for {name}")
            
            # Nodes don't have owners or namespaces
            node_dict["owner_chain"] = []
            
            return node_dict

        except ApiException as e:
            if e.status == 404:
                return None
            else:
                raise

    def fetch_pvc_snapshot(self, namespace: str, name: str) -> dict | None:
        """
        Fetch a single PersistentVolumeClaim and build a snapshot dict for analyzers.
        
        Returns:
            dict: PVC snapshot
            None: Only if not found (404)
        
        Raises:
            Exception: On unexpected API errors (caller should handle)
        """
        try:
            pvc = self.api.read_namespaced_persistent_volume_claim(name=name, namespace=namespace)
            pvc_dict = pvc.to_dict()
            
            # Ensure minimal required structure
            if not pvc_dict or "metadata" not in pvc_dict:
                raise ValueError(f"Invalid PVC structure returned from API for {namespace}/{name}")
            
            # PVCs typically don't have owners (they're owned by StatefulSets sometimes)
            pvc_dict["owner_chain"] = self.resolve_owners(pvc_dict)
            
            return pvc_dict

        except ApiException as e:
            if e.status == 404:
                return None
            else:
                raise

    def fetch_statefulset_snapshot(self, namespace: str, name: str) -> dict | None:
        """
        Fetch a StatefulSet snapshot.

        StatefulSets are used for stateful workloads (databases, message queues).
        They give pods stable network identities and persistent storage.
        K8s emits Warnings when the StatefulSet can't scale or a pod fails to come up.
        """
        try:
            sts = self.apps_v1.read_namespaced_stateful_set(name=name, namespace=namespace)
            sts_dict = sts.to_dict()
            if not sts_dict or "metadata" not in sts_dict:
                raise ValueError(f"Invalid StatefulSet structure for {namespace}/{name}")
            sts_dict["owner_chain"] = []  # StatefulSets are typically top-level
            return sts_dict
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def fetch_daemonset_snapshot(self, namespace: str, name: str) -> dict | None:
        """
        Fetch a DaemonSet snapshot.

        DaemonSets run exactly one pod per node (log agents, monitoring, CNI plugins).
        K8s emits Warnings when a pod can't be scheduled on a node (e.g. taints,
        insufficient resources, or node is not ready).
        """
        try:
            ds = self.apps_v1.read_namespaced_daemon_set(name=name, namespace=namespace)
            ds_dict = ds.to_dict()
            if not ds_dict or "metadata" not in ds_dict:
                raise ValueError(f"Invalid DaemonSet structure for {namespace}/{name}")
            ds_dict["owner_chain"] = []
            return ds_dict
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def fetch_job_snapshot(self, namespace: str, name: str) -> dict | None:
        """
        Fetch a Job snapshot.

        Jobs run pods until they succeed. Common failures:
        - BackoffLimitExceeded: job retried N times and never succeeded
        - DeadlineExceeded: job hit its activeDeadlineSeconds timeout
        These are different from Deployment failures — they mean a one-shot task
        (migration, backup, ML training run) permanently failed.
        """
        try:
            job = self.batch_v1.read_namespaced_job(name=name, namespace=namespace)
            job_dict = job.to_dict()
            if not job_dict or "metadata" not in job_dict:
                raise ValueError(f"Invalid Job structure for {namespace}/{name}")
            # Jobs can be owned by CronJobs — resolve that owner chain
            job_dict["owner_chain"] = self.resolve_owners(job_dict)
            return job_dict
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def fetch_replicaset_parent_deployment(self, namespace: str, name: str) -> dict | None:
        """
        Fetch ReplicaSet and return its parent Deployment snapshot.
        Used when ReplicaSet events (e.g., FailedCreate) indicate Deployment issues.
        
        Returns:
            dict: Parent Deployment snapshot (or None if not found/no parent)
        """
        try:
            # Fetch the ReplicaSet
            rs = self.apps_v1.read_namespaced_replica_set(name=name, namespace=namespace)
            rs_dict = rs.to_dict()
            
            # Extract parent Deployment from ownerReferences
            owner_refs = rs_dict.get("metadata", {}).get("owner_references") or []
            deployment_ref = None
            
            for ref in owner_refs:
                if ref.get("kind") == "Deployment":
                    deployment_ref = ref
                    break
            
            if not deployment_ref:
                print(f"[SnapshotFetcher] ReplicaSet {namespace}/{name} has no Deployment owner")
                return None
            
            deployment_name = deployment_ref.get("name")
            if not deployment_name:
                return None
            
            # Fetch parent Deployment
            return self.fetch_deployment_snapshot(namespace, deployment_name)
            
        except ApiException as e:
            if e.status == 404:
                return None
            else:
                raise

    def _fetch_failing_container_logs(self, namespace: str, pod_name: str, pod_dict: dict) -> str | None:
        """Return logs from the container currently in an error state, falling back to the first container."""
        _ERROR_WAITING  = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
                           "ErrImageNeverPull", "OOMKilled", "Error"}
        _ERROR_TERM     = {"OOMKilled", "Error", "ContainerCannotRun"}

        all_statuses = (
            (pod_dict.get("status") or {}).get("container_statuses") or []
        ) + (
            (pod_dict.get("status") or {}).get("init_container_statuses") or []
        )

        container_name = None
        for cs in all_statuses:
            waiting_reason = ((cs.get("state") or {}).get("waiting") or {}).get("reason", "")
            term_reason    = ((cs.get("last_state") or {}).get("terminated") or {}).get("reason", "")
            if waiting_reason in _ERROR_WAITING or term_reason in _ERROR_TERM:
                container_name = cs.get("name")
                break

        if not container_name:
            regular = (pod_dict.get("status") or {}).get("container_statuses") or []
            if regular:
                container_name = regular[0].get("name")

        if not container_name:
            return None

        try:
            return self.fetch_pod_logs(namespace, pod_name, container_name)
        except Exception:
            return None

    def resolve_owners(self, resource: dict) -> list[str]:
        """
        Follow owner chain (Pod → ReplicaSet → Deployment → ...).
        Returns list of owner identifiers in format: ["Kind/namespace/name", ...]
        
        Example:
            Pod "api-server-abc123" → ["ReplicaSet/default/api-server-xyz", "Deployment/default/api-server"]
        
        This allows findings to be reported at the right level:
            "Deployment api-server is unavailable" (not "Pod api-server-abc123 crashed")
        """
        owners = []
        current = resource
        depth = 0
        
        while current and depth < self.MAX_OWNER_DEPTH:
            owner_refs = current.get("metadata", {}).get("owner_references") or []
            if not owner_refs:
                break
            
            # Take the first owner (usually the controlling owner)
            owner_ref = owner_refs[0]
            owner_kind = owner_ref.get("kind")
            owner_name = owner_ref.get("name")
            namespace = current.get("metadata", {}).get("namespace")
            
            if not owner_kind or not owner_name:
                break
            
            owner_identifier = f"{owner_kind}/{namespace}/{owner_name}"
            owners.append(owner_identifier)
            
            # Fetch the owner resource to continue chain
            try:
                current = self._fetch_owner_resource(owner_kind, namespace, owner_name)
            except Exception:
                # Can't fetch owner (doesn't exist, not supported kind, etc.)
                break
            
            depth += 1
        
        return owners

    def _fetch_owner_resource(self, kind: str, namespace: str, name: str) -> dict | None:
        """Fetch owner resource by kind."""
        try:
            if kind == "ReplicaSet":
                rs = self.apps_v1.read_namespaced_replica_set(name, namespace)
                return rs.to_dict()
            elif kind == "Deployment":
                deploy = self.apps_v1.read_namespaced_deployment(name, namespace)
                return deploy.to_dict()
            elif kind == "StatefulSet":
                sts = self.apps_v1.read_namespaced_stateful_set(name, namespace)
                return sts.to_dict()
            elif kind == "DaemonSet":
                ds = self.apps_v1.read_namespaced_daemon_set(name, namespace)
                return ds.to_dict()
            else:
                # Unsupported owner kind
                return None
        except ApiException:
            return None

    def fetch_pod_logs(self, namespace: str, pod_name: str, container_name: str = None) -> str | None:
        """
        Fetch bounded logs from a pod container.
        
        Args:
            namespace: Pod namespace
            pod_name: Pod name
            container_name: Container name (if None, uses first container)
        
        Returns:
            Last MAX_LOG_LINES lines of logs with timestamps, or None if unavailable
        """
        try:
            logs = self.api.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                container=container_name,
                tail_lines=self.MAX_LOG_LINES,
                timestamps=True
            )
            return logs
        except ApiException as e:
            # Logs might not be available (container not started, terminated, etc.)
            return None
