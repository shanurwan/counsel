# The support pool: the agent that reads customer tickets runs in its own Cloud
# Run service, as its own service account, logged in as its own database login
# (counsel_runtime_support). The core runtime's login cannot become it, and this
# service cannot read the core runtime's secrets (docs/red-team.md, R3).
#
# Same image and sizing as the core runtime: scale to zero, one instance, half a
# vCPU, and a second Scheduler job (3 are free). Both services together have the
# worst case one service had before (docs/free-tier.md).

resource "google_service_account" "support" {
  account_id   = "counsel-runtime-support"
  display_name = "Counsel support pool runtime (Cloud Run)"
  depends_on   = [google_project_service.apis]
}

# Its own database connection string, and the model key. Nothing of the core runtime's.
resource "google_secret_manager_secret" "support" {
  secret_id = "support-database-url"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "support_database" {
  secret_id = google_secret_manager_secret.support.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.support.member
}

resource "google_secret_manager_secret_iam_member" "support_model" {
  secret_id = google_secret_manager_secret.secrets["nvidia-api-key"].id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.support.member
}

resource "google_cloud_run_v2_service" "support" {
  name                = "counsel-support"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL" # IAM still required: no allUsers invoker
  deletion_protection = false

  template {
    service_account                  = google_service_account.support.email
    timeout                          = "120s"
    max_instance_request_concurrency = 1

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      # The runtime's current image; CI deploys each new one to every service (ignored below).
      image = google_cloud_run_v2_service.counsel.template[0].containers[0].image

      resources {
        limits = {
          cpu    = "0.5" # see docs/free-tier.md: two services at 0.5 cost what one at 1 did
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = false
      }

      env {
        name  = "COUNSEL_RUNTIME_POOL"
        value = "support"
      }
      env {
        name  = "COUNSEL_MODEL"
        value = "live"
      }
      env {
        name = "COUNSEL_DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.support.secret_id
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
      scaling,
    ]
  }

  depends_on = [google_secret_manager_secret_iam_member.support_database,
  google_secret_manager_secret_iam_member.support_model]
}

resource "google_cloud_run_v2_service_iam_member" "support_scheduler" {
  name     = google_cloud_run_v2_service.support.name
  location = var.region
  role     = "roles/run.invoker"
  member   = google_service_account.scheduler.member
}

# The second of three free Scheduler jobs, a quarter hour after the core tick.
resource "google_cloud_scheduler_job" "support_tick" {
  name             = "counsel-tick-support"
  region           = var.region
  schedule         = "15,45 * * * *"
  time_zone        = "Etc/UTC"
  attempt_deadline = "150s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.support.uri}/tick"
    oidc_token {
      service_account_email = google_service_account.scheduler.email
      audience              = google_cloud_run_v2_service.support.uri
    }
  }
}

# The deploy job updates this service too, and runs it as the support account.
resource "google_cloud_run_v2_service_iam_member" "deployer_support" {
  name     = google_cloud_run_v2_service.support.name
  location = var.region
  role     = "roles/run.developer"
  member   = google_service_account.deployer.member
}

resource "google_service_account_iam_member" "deployer_acts_as_support" {
  service_account_id = google_service_account.support.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.deployer.member
}
