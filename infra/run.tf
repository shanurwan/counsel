# The runtime on Cloud Run, its one Scheduler tick, and keyless deploys from
# GitHub Actions. Sizing keeps the worst case inside the free tier
# (docs/free-tier.md): 1 instance, 1 request at a time, 120 s timeout,
# 1 vCPU, 512 MiB, CPU only while a request is in flight.

variable "github_repository" {
  type    = string
  default = "shanurwan/counsel"
}

# The numeric id survives renames and cannot be taken over by someone who
# recreates a deleted repository with the same name.
variable "github_repository_id" {
  type    = string
  default = "1442975741"
}

resource "google_cloud_run_v2_service" "counsel" {
  name                = "counsel"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL" # IAM still required: no allUsers invoker
  deletion_protection = false

  template {
    service_account                  = google_service_account.runtime.email
    timeout                          = "120s"
    max_instance_request_concurrency = 1

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      # Bootstrap image; CI deploys the real one (ignored below).
      image = "us-docker.pkg.dev/cloudrun/container/hello"

      resources {
        limits = {
          # Half a vCPU: with the support pool's service at the same size, the
          # worst case for both stays where one service at 1 vCPU was (docs/free-tier.md).
          cpu    = "0.5"
          memory = "512Mi"
        }
        cpu_idle          = true # request-based billing
        startup_cpu_boost = false
      }

      env {
        name  = "COUNSEL_MODEL"
        value = "live"
      }
      env {
        name = "COUNSEL_DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["database-url"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "NVIDIA_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.secrets["nvidia-api-key"].secret_id
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
      # The API reports an empty scaling block Terraform never set.
      scaling,
    ]
  }

  depends_on = [google_secret_manager_secret_iam_member.runtime]
}

# --- the clock -------------------------------------------------------------------

resource "google_service_account" "scheduler" {
  account_id   = "counsel-scheduler"
  display_name = "Calls the Counsel runtime every 30 minutes"
  depends_on   = [google_project_service.apis]
}

resource "google_cloud_run_v2_service_iam_member" "scheduler" {
  name     = google_cloud_run_v2_service.counsel.name
  location = var.region
  role     = "roles/run.invoker"
  member   = google_service_account.scheduler.member
}

# The core pool's Scheduler job (3 are free; infra/pools.tf uses a second). Agent schedules live in agents/*.yaml;
# the runtime decides what is due on each tick.
resource "google_cloud_scheduler_job" "tick" {
  name             = "counsel-tick"
  region           = var.region
  schedule         = "*/30 * * * *"
  time_zone        = "Etc/UTC"
  attempt_deadline = "150s"

  retry_config {
    retry_count = 0 # the next tick picks up anything left behind
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.counsel.uri}/tick"
    oidc_token {
      service_account_email = google_service_account.scheduler.email
      audience              = google_cloud_run_v2_service.counsel.uri
    }
  }
}

# --- keyless deploys from GitHub Actions -----------------------------------------

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
  depends_on                = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  # checkov:skip=CKV_GCP_125:The check's regex predates GitHub's immutable subject (owner@id/repo@id); the condition pins the exact sub, ref and repository_id.
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }
  # Only pushes to main of this one repository can ever get a token. The repo
  # uses GitHub's immutable subject, which pins owner and repository by id.
  attribute_condition = "assertion.sub == 'repo:shanurwan@176239359/counsel@1442975741:ref:refs/heads/main' && assertion.repository_id == '${var.github_repository_id}'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "deployer" {
  account_id   = "counsel-deployer"
  display_name = "Deploys Counsel from GitHub Actions"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account_iam_member" "deployer_wif" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}

# Push images to one repository, update one service, and run it as the runtime
# account. Nothing project-wide.
resource "google_artifact_registry_repository_iam_member" "deployer" {
  location   = var.region
  repository = google_artifact_registry_repository.images["counsel"].repository_id
  role       = "roles/artifactregistry.writer"
  member     = google_service_account.deployer.member
}

resource "google_cloud_run_v2_service_iam_member" "deployer" {
  name     = google_cloud_run_v2_service.counsel.name
  location = var.region
  role     = "roles/run.developer"
  member   = google_service_account.deployer.member
}

resource "google_service_account_iam_member" "deployer_acts_as_runtime" {
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.deployer.member
}

output "service_url" {
  value = google_cloud_run_v2_service.counsel.uri
}

output "workload_identity_provider" {
  value = google_iam_workload_identity_pool_provider.github.name
}

output "deployer_service_account" {
  value = google_service_account.deployer.email
}

# The admin connection string for `counsel migrate`, read by the deploy job only.
# Not in the runtime's secret set: the runtime logs in as counsel_runtime, which
# holds no privileges; only a deploy from main can reach this.
resource "google_secret_manager_secret" "migrate" {
  secret_id = "migrate-database-url"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "deployer_migrate" {
  secret_id = google_secret_manager_secret.migrate.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.deployer.member
}
