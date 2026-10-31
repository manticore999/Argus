locals {
  labels = {
    "app.kubernetes.io/name"       = "argus"
    "app.kubernetes.io/managed-by" = "terraform"
  }

  data_path = "/data"

  prometheus_url = var.enable_prometheus ? "http://prometheus-server.${var.namespace}.svc.cluster.local" : var.prometheus_url
}

resource "kubernetes_namespace_v1" "argus" {
  metadata {
    name   = var.namespace
    labels = local.labels
  }
}

resource "kubernetes_secret_v1" "gemini" {
  metadata {
    name      = "argus-gemini"
    namespace = kubernetes_namespace_v1.argus.metadata[0].name
    labels    = local.labels
  }

  data = {
    GEMINI_API_KEYS = var.gemini_api_keys
  }
}

resource "kubernetes_persistent_volume_claim_v1" "data" {
  metadata {
    name      = "argus-data"
    namespace = kubernetes_namespace_v1.argus.metadata[0].name
    labels    = local.labels
  }

  spec {
    access_modes = ["ReadWriteOnce"]

    resources {
      requests = {
        storage = var.storage_size
      }
    }
  }

  wait_until_bound = false
}

# One replica on purpose: findings and incidents live in a single SQLite file.
resource "kubernetes_deployment_v1" "argus" {
  metadata {
    name      = "argus"
    namespace = kubernetes_namespace_v1.argus.metadata[0].name
    labels    = local.labels
  }

  spec {
    replicas = 1

    strategy {
      type = "Recreate"
    }

    selector {
      match_labels = {
        "app.kubernetes.io/name" = "argus"
      }
    }

    template {
      metadata {
        labels = local.labels
      }

      spec {
        service_account_name = kubernetes_service_account_v1.argus.metadata[0].name

        security_context {
          run_as_non_root = true
          run_as_user     = 10001
          fs_group        = 10001
        }

        container {
          name              = "argus"
          image             = var.image
          image_pull_policy = var.image_pull_policy

          port {
            name           = "http"
            container_port = 8080
          }

          env {
            name = "GEMINI_API_KEYS"

            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.gemini.metadata[0].name
                key  = "GEMINI_API_KEYS"
              }
            }
          }

          env {
            name  = "GEMINI_MODEL"
            value = var.gemini_model
          }

          env {
            name  = "PROMETHEUS_URL"
            value = local.prometheus_url
          }

          env {
            name  = "DB_PATH"
            value = "${local.data_path}/aiops.db"
          }

          volume_mount {
            name       = "data"
            mount_path = local.data_path
          }

          readiness_probe {
            http_get {
              path = "/api/stats"
              port = "http"
            }

            initial_delay_seconds = 10
            period_seconds        = 10
          }

          liveness_probe {
            http_get {
              path = "/api/stats"
              port = "http"
            }

            initial_delay_seconds = 30
            period_seconds        = 30
          }

          resources {
            requests = {
              cpu    = "100m"
              memory = "256Mi"
            }
            limits = {
              memory = "512Mi"
            }
          }

          security_context {
            allow_privilege_escalation = false

            capabilities {
              drop = ["ALL"]
            }
          }
        }

        volume {
          name = "data"

          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim_v1.data.metadata[0].name
          }
        }
      }
    }
  }

  depends_on = [
    kubernetes_cluster_role_binding_v1.read,
    helm_release.prometheus,
  ]
}

resource "kubernetes_service_v1" "argus" {
  metadata {
    name      = "argus"
    namespace = kubernetes_namespace_v1.argus.metadata[0].name
    labels    = local.labels
  }

  spec {
    type = var.service_type

    selector = {
      "app.kubernetes.io/name" = "argus"
    }

    port {
      name        = "http"
      port        = 8080
      target_port = "http"
    }
  }
}
