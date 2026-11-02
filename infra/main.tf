# Foundation for Counsel on the GCP always-free tier.
#
# Free tier, per billing account per month (see docs/free-tier.md):
#   Cloud Run          2M requests, 180k vCPU-s, 360k GiB-s, 1 GB egress from North America
#   Cloud Scheduler    3 jobs             -> Counsel uses 1 (a clock tick)
#   Secret Manager     6 active versions  -> Counsel uses 2
#   BigQuery           10 GiB storage, 1 TiB queries
#   Artifact Registry  0.5 GB             -> cleanup keeps the latest 2 images
#   Cloud Storage      5 GB in US regions
#   Cloud Build        2,500 build-minutes (only the billing guard builds here)
#   Pub/Sub            10 GiB

data "google_project" "this" {}

locals {
  apis = [
    "artifactregistry.googleapis.com",
    "bigquery.googleapis.com",
    "billingbudgets.googleapis.com",
    "cloudbilling.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudfunctions.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudscheduler.googleapis.com",
    "eventarc.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "serviceusage.googleapis.com",
    "sts.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

# --- images -------------------------------------------------------------------

resource "google_artifact_registry_repository" "images" {
  # checkov:skip=CKV_GCP_84:Cloud KMS keys are billed; this project stays on the free tier. Google-managed encryption at rest applies.
  for_each      = toset(["counsel", "functions"])
  repository_id = each.value
  location      = var.region
  format        = "DOCKER"

  cleanup_policy_dry_run = false
  cleanup_policies {
    id     = "keep-latest-2"
    action = "KEEP"
    most_recent_versions {
      keep_count = 2
    }
  }
  cleanup_policies {
    id     = "delete-older"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "86400s"
    }
  }

  depends_on = [google_project_service.apis]
}

# --- secrets (values are added with gcloud, never through Terraform state) ----

resource "google_secret_manager_secret" "secrets" {
  for_each  = toset(["database-url", "nvidia-api-key"])
  secret_id = each.value
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# --- run warehouse ---------------------------------------------------------------

resource "google_bigquery_dataset" "runs" {
  # checkov:skip=CKV_GCP_81:Cloud KMS keys are billed; this project stays on the free tier. Google-managed encryption at rest applies.
  dataset_id  = "counsel_runs"
  location    = "US"
  description = "Every run, step, prompt, completion and tool call, for replay and reporting."

  # Partitions older than 90 days are dropped, keeping storage far below 10 GiB.
  default_partition_expiration_ms = 90 * 24 * 60 * 60 * 1000

  depends_on = [google_project_service.apis]
}

# --- identities ------------------------------------------------------------------

resource "google_service_account" "runtime" {
  account_id   = "counsel-runtime"
  display_name = "Counsel runtime (Cloud Run)"
  depends_on   = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "runtime" {
  for_each  = google_secret_manager_secret.secrets
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.runtime.member
}

resource "google_bigquery_dataset_iam_member" "runtime" {
  dataset_id = google_bigquery_dataset.runs.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_service_account.runtime.member
}

resource "google_project_iam_member" "runtime_jobs" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = google_service_account.runtime.member
}
