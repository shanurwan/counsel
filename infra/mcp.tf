# The hosted MCP server (docs/mcp.md): the runtime's image, started as
# `counsel-mcp --http`. Open at the network level, because MCP clients connect
# from wherever their user is; every request needs an access token issued by this
# project's Supabase Auth, checked in the app before any tool runs.
#
# It has its own identity and its own secret: it cannot read the runtime's
# database URL or model key, and the runtime cannot read its. It logs in as
# counsel_mcp, which `counsel audit` holds to the journal, one question insert and
# the two person-scoped functions. Sizing stays inside the free tier
# (docs/free-tier.md): scale to zero, one instance, CPU only during requests.

variable "supabase_auth_issuer" {
  type        = string
  description = "Supabase Auth issuer, https://<project-ref>.supabase.co/auth/v1"
}

locals {
  # Cloud Run's deterministic URL, known before the service exists, so the
  # server can name itself as the resource in its protected resource metadata.
  mcp_url = "https://counsel-mcp-${data.google_project.this.number}.${var.region}.run.app"
}

resource "google_service_account" "mcp" {
  account_id   = "counsel-mcp"
  display_name = "Counsel hosted MCP server (Cloud Run)"
  depends_on   = [google_project_service.apis]
}

resource "google_secret_manager_secret" "mcp" {
  secret_id = "mcp-database-url"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "mcp" {
  secret_id = google_secret_manager_secret.mcp.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.mcp.member
}

resource "google_cloud_run_v2_service" "mcp" {
  name                = "counsel-mcp"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account = google_service_account.mcp.email
    timeout         = "30s"
    # counsel_mcp holds at most 3 connections, one per tool call.
    max_instance_request_concurrency = 3

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      # Starts on the image the runtime runs now (the placeholder image has no
      # counsel-mcp); CI deploys each new one to both services (ignored below).
      image   = google_cloud_run_v2_service.counsel.template[0].containers[0].image
      command = ["counsel-mcp"]
      args    = ["--http"]

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = false
      }

      env {
        name  = "COUNSEL_AUTH_ISSUER"
        value = var.supabase_auth_issuer
      }
      env {
        name  = "COUNSEL_MCP_PUBLIC_URL"
        value = "${local.mcp_url}/mcp"
      }
      env {
        name = "COUNSEL_MCP_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.mcp.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
      scaling,
    ]
  }

  depends_on = [google_secret_manager_secret_iam_member.mcp]
}

resource "google_cloud_run_v2_service_iam_member" "mcp_public" {
  # checkov:skip=CKV_GCP_102:An MCP server must be reachable by MCP clients. Every request is authenticated in the app with a Supabase Auth access token; there is no unauthenticated tool.
  name     = google_cloud_run_v2_service.mcp.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# The deploy job updates this service too, and runs it as the MCP account.
resource "google_cloud_run_v2_service_iam_member" "deployer_mcp" {
  name     = google_cloud_run_v2_service.mcp.name
  location = var.region
  role     = "roles/run.developer"
  member   = google_service_account.deployer.member
}

resource "google_service_account_iam_member" "deployer_acts_as_mcp" {
  service_account_id = google_service_account.mcp.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.deployer.member
}

output "mcp_url" {
  value = "${local.mcp_url}/mcp"
}
