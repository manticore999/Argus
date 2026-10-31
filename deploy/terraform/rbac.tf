resource "kubernetes_service_account_v1" "argus" {
  metadata {
    name      = "argus"
    namespace = kubernetes_namespace_v1.argus.metadata[0].name
    labels    = local.labels
  }
}

# Watchers, analyzers and the diagnostic agent only read the cluster.
resource "kubernetes_cluster_role_v1" "read" {
  metadata {
    name   = "argus-read"
    labels = local.labels
  }

  rule {
    api_groups = [""]
    resources = [
      "endpoints",
      "events",
      "namespaces",
      "nodes",
      "persistentvolumeclaims",
      "pods",
      "pods/log",
      "services",
    ]
    verbs = ["get", "list", "watch"]
  }

  rule {
    api_groups = ["apps"]
    resources  = ["daemonsets", "deployments", "replicasets", "statefulsets"]
    verbs      = ["get", "list", "watch"]
  }

  rule {
    api_groups = ["batch"]
    resources  = ["jobs"]
    verbs      = ["get", "list", "watch"]
  }
}

resource "kubernetes_cluster_role_binding_v1" "read" {
  metadata {
    name   = "argus-read"
    labels = local.labels
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.read.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.argus.metadata[0].name
    namespace = kubernetes_namespace_v1.argus.metadata[0].name
  }
}

# Write access is limited to the three whitelisted actions in engine/action_executor.py:
# restart_pod, scale_deployment and rollout_restart_deployment.
resource "kubernetes_cluster_role_v1" "remediate" {
  count = var.enable_remediation ? 1 : 0

  metadata {
    name   = "argus-remediate"
    labels = local.labels
  }

  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["delete"]
  }

  rule {
    api_groups = ["apps"]
    resources  = ["deployments", "deployments/scale"]
    verbs      = ["patch"]
  }
}

resource "kubernetes_cluster_role_binding_v1" "remediate" {
  count = var.enable_remediation ? 1 : 0

  metadata {
    name   = "argus-remediate"
    labels = local.labels
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.remediate[0].metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.argus.metadata[0].name
    namespace = kubernetes_namespace_v1.argus.metadata[0].name
  }
}
