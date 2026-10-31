output "namespace" {
  description = "Namespace that holds Argus."
  value       = kubernetes_namespace_v1.argus.metadata[0].name
}

output "dashboard_port_forward" {
  description = "Command that opens the dashboard on http://localhost:8080."
  value       = "kubectl port-forward -n ${kubernetes_namespace_v1.argus.metadata[0].name} svc/${kubernetes_service_v1.argus.metadata[0].name} 8080:8080"
}

output "prometheus_url" {
  description = "Prometheus URL used by Argus."
  value       = local.prometheus_url
}
