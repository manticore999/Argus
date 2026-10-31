resource "helm_release" "prometheus" {
  count = var.enable_prometheus ? 1 : 0

  name       = "prometheus"
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "prometheus"
  version    = var.prometheus_chart_version
  namespace  = kubernetes_namespace_v1.argus.metadata[0].name

  # Argus reads metrics directly, so the alerting components stay off.
  set {
    name  = "alertmanager.enabled"
    value = "false"
  }

  set {
    name  = "prometheus-pushgateway.enabled"
    value = "false"
  }

  set {
    name  = "server.persistentVolume.enabled"
    value = "false"
  }
}
