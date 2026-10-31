variable "kubeconfig_path" {
  description = "Path to the kubeconfig file."
  type        = string
  default     = "~/.kube/config"
}

variable "kube_context" {
  description = "Kubeconfig context to deploy to."
  type        = string
  default     = "minikube"
}

variable "namespace" {
  description = "Namespace that holds Argus and Prometheus."
  type        = string
  default     = "argus"
}

variable "image" {
  description = "Argus container image."
  type        = string
  default     = "argus:latest"
}

variable "image_pull_policy" {
  description = "Use IfNotPresent for an image loaded into Minikube, Always for a registry."
  type        = string
  default     = "IfNotPresent"

  validation {
    condition     = contains(["Always", "IfNotPresent", "Never"], var.image_pull_policy)
    error_message = "image_pull_policy must be Always, IfNotPresent or Never."
  }
}

variable "gemini_api_keys" {
  description = "Comma-separated Gemini API keys, rotated by Argus on rate limits."
  type        = string
  sensitive   = true
}

variable "gemini_model" {
  description = "Gemini model used for diagnosis."
  type        = string
  default     = "gemini-2.5-flash-lite"
}

variable "storage_size" {
  description = "Size of the volume that holds the SQLite database."
  type        = string
  default     = "1Gi"
}

variable "service_type" {
  description = "Kubernetes service type for the dashboard."
  type        = string
  default     = "ClusterIP"

  validation {
    condition     = contains(["ClusterIP", "NodePort", "LoadBalancer"], var.service_type)
    error_message = "service_type must be ClusterIP, NodePort or LoadBalancer."
  }
}

variable "enable_remediation" {
  description = "Grant the write access needed by the approved actions. When false, Argus can only observe the cluster."
  type        = bool
  default     = true
}

variable "enable_prometheus" {
  description = "Install Prometheus with Helm. When false, set prometheus_url to an existing server."
  type        = bool
  default     = true
}

variable "prometheus_chart_version" {
  description = "Version of the prometheus-community/prometheus chart."
  type        = string
  default     = "25.27.0"
}

variable "prometheus_url" {
  description = "URL of an existing Prometheus server, used when enable_prometheus is false."
  type        = string
  default     = ""
}
