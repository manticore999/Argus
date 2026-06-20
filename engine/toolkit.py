from __future__ import annotations

import time
import logging
from typing import Any

from kubernetes.client import CoreV1Api, AppsV1Api, CustomObjectsApi, V1Scale
from kubernetes.client.exceptions import ApiException

from k8s.client import load_kube_client

logger = logging.getLogger("argus.toolkit")

class KubernetesToolkit:
    """
    A unified, object-oriented toolkit for Kubernetes operations.
    Provides curated diagnostics and remediation actions for the LLM Agent.
    """

    def __init__(self):
        self.v1 = load_kube_client()
        # Create AppsV1Api using the same ApiClient initialized in load_kube_client
        self.apps_v1 = AppsV1Api(self.v1.api_client)
        self.custom_api = CustomObjectsApi(self.v1.api_client)

    # =========================================================================
    # DIAGNOSTIC TOOLS (Read-Only)
    # =========================================================================

    def get_pod_logs(self, namespace: str, pod_name: str, lines: int = 200) -> dict[str, Any]:
        """Fetch the last N lines of logs for a pod."""
        try:
            logs = self.v1.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                tail_lines=lines,
                _request_timeout=10
            )
            return {"success": True, "logs": logs}
        except ApiException as e:
            return {"success": False, "error": str(e)}

    def get_pod_events(self, namespace: str, pod_name: str) -> dict[str, Any]:
        """Fetch Kubernetes events associated with a specific pod."""
        try:
            field_selector = f"involvedObject.name={pod_name},involvedObject.kind=Pod"
            events = self.v1.list_namespaced_event(namespace=namespace, field_selector=field_selector)
            event_list = [
                {"reason": e.reason, "message": e.message, "type": e.type, "time": str(e.last_timestamp)}
                for e in events.items
            ]
            return {"success": True, "events": event_list}
        except ApiException as e:
            return {"success": False, "error": str(e)}

    def get_pod_status(self, namespace: str, pod_name: str) -> dict[str, Any]:
        """Get the full status, phase, and restart counts for a pod."""
        try:
            pod = self.v1.read_namespaced_pod(name=pod_name, namespace=namespace)
            status = pod.status
            container_statuses = []
            if status.container_statuses:
                for cs in status.container_statuses:
                    state = "Running"
                    reason = ""
                    if cs.state.waiting:
                        state = "Waiting"
                        reason = cs.state.waiting.reason
                    elif cs.state.terminated:
                        state = "Terminated"
                        reason = cs.state.terminated.reason
                    
                    container_statuses.append({
                        "name": cs.name,
                        "ready": cs.ready,
                        "restart_count": cs.restart_count,
                        "state": state,
                        "reason": reason
                    })
            return {
                "success": True,
                "phase": status.phase,
                "host_ip": status.host_ip,
                "pod_ip": status.pod_ip,
                "containers": container_statuses
            }
        except ApiException as e:
            return {"success": False, "error": str(e)}

    def get_deployment_status(self, namespace: str, deployment_name: str) -> dict[str, Any]:
        """Get replica counts, conditions, and status for a deployment."""
        try:
            dep = self.apps_v1.read_namespaced_deployment(name=deployment_name, namespace=namespace)
            conditions = [{"type": c.type, "status": c.status, "reason": c.reason} for c in (dep.status.conditions or [])]
            return {
                "success": True,
                "replicas": dep.spec.replicas,
                "ready_replicas": dep.status.ready_replicas or 0,
                "unavailable_replicas": dep.status.unavailable_replicas or 0,
                "conditions": conditions
            }
        except ApiException as e:
            return {"success": False, "error": str(e)}

    # =========================================================================
    # ACTION TOOLS (Mutating - Requires Approval)
    # =========================================================================

    def restart_pod(self, namespace: str, pod_name: str) -> dict[str, Any]:
        """
        Force restart a pod by deleting it. 
        (If managed by a deployment/replicaset, it will be recreated).
        """
        try:
            self.v1.delete_namespaced_pod(name=pod_name, namespace=namespace)
            return {"success": True, "message": f"Pod {pod_name} deleted successfully."}
        except ApiException as e:
            return {"success": False, "error": str(e)}

    def scale_deployment(self, namespace: str, deployment_name: str, replicas: int) -> dict[str, Any]:
        """Scale a deployment to the desired number of replicas."""
        try:
            scale = V1Scale(
                spec={"replicas": replicas},
                metadata={"name": deployment_name, "namespace": namespace}
            )
            self.apps_v1.patch_namespaced_deployment_scale(
                name=deployment_name,
                namespace=namespace,
                body=scale
            )
            return {"success": True, "message": f"Deployment {deployment_name} scaled to {replicas}."}
        except ApiException as e:
            return {"success": False, "error": str(e)}

    def rollout_restart_deployment(self, namespace: str, deployment_name: str) -> dict[str, Any]:
        """Trigger a safe rolling restart of a deployment."""
        try:
            # Patch deployment spec template with an annotation to force restart
            patch = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "argus.aiops/restartedAt": str(time.time())
                            }
                        }
                    }
                }
            }
            self.apps_v1.patch_namespaced_deployment(
                name=deployment_name,
                namespace=namespace,
                body=patch
            )
            return {"success": True, "message": f"Rollout restart triggered for {deployment_name}."}
        except ApiException as e:
            return {"success": False, "error": str(e)}
