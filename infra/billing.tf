# Budget alert plus a hard stop.
#
# An alert email goes out as soon as the project spends anything (1% of the
# budget). At 100% the billing guard unlinks billing, which stops every paid
# service. Budget data lags usage by hours, so the real ceiling is the budget
# plus a few hours of spend.

resource "google_pubsub_topic" "billing_alerts" {
  # checkov:skip=CKV_GCP_83:Cloud KMS keys are billed; this project stays on the free tier. Google-managed encryption at rest applies.
  name       = "billing-alerts"
  depends_on = [google_project_service.apis]
}

resource "google_billing_budget" "hard_stop" {
  billing_account = var.billing_account
  display_name    = "counsel hard stop"

  budget_filter {
    projects               = ["projects/${data.google_project.this.number}"]
    credit_types_treatment = "INCLUDE_ALL_CREDITS"
  }

  amount {
    specified_amount {
      currency_code = var.budget_currency
      units         = tostring(var.hard_stop_amount)
    }
  }

  dynamic "threshold_rules" {
    for_each = [0.01, 0.5, 1.0]
    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "CURRENT_SPEND"
    }
  }

  all_updates_rule {
    pubsub_topic   = google_pubsub_topic.billing_alerts.id
    schema_version = "1.0"
    # Billing account admins still get the threshold emails.
    disable_default_iam_recipients = false
  }

  depends_on = [google_project_service.apis]
}

# --- the guard function ------------------------------------------------------

resource "google_service_account" "billing_guard" {
  account_id   = "billing-guard"
  display_name = "Unlinks billing when the hard stop is reached"
  depends_on   = [google_project_service.apis]
}

# The narrowest roles that can read and unlink billing on this one project:
# projectManager holds only create/deleteBillingAssignment, and reading
# billingInfo needs resourcemanager.projects.get (browser, read-only).
resource "google_project_iam_member" "billing_guard" {
  for_each = toset(["roles/billing.projectManager", "roles/browser"])
  project  = var.project_id
  role     = each.value
  member   = google_service_account.billing_guard.member
}

resource "google_service_account" "builder" {
  account_id   = "functions-builder"
  display_name = "Builds Cloud Run functions"
  depends_on   = [google_project_service.apis]
}

resource "google_project_iam_member" "builder" {
  for_each = toset([
    "roles/artifactregistry.writer",
    "roles/logging.logWriter",
    "roles/storage.objectViewer",
  ])
  project = var.project_id
  role    = each.value
  member  = google_service_account.builder.member
}

resource "google_storage_bucket" "function_source" {
  # checkov:skip=CKV_GCP_62:Holds only the billing guard's build zips, deleted after 7 days; access logs would need a second bucket for no signal.
  # checkov:skip=CKV_GCP_78:Objects are content-addressed and deleted on purpose after 7 days; versioning would only retain deleted builds.
  name                        = "${var.project_id}-function-source"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "Delete"
    }
  }
}

data "archive_file" "billing_guard" {
  type        = "zip"
  source_dir  = "${path.module}/billing_guard"
  output_path = "${path.module}/.build/billing_guard.zip"
}

resource "google_storage_bucket_object" "billing_guard" {
  name   = "billing_guard-${data.archive_file.billing_guard.output_md5}.zip"
  bucket = google_storage_bucket.function_source.name
  source = data.archive_file.billing_guard.output_path
}

resource "google_cloudfunctions2_function" "billing_guard" {
  name     = "billing-guard"
  location = var.region

  build_config {
    runtime           = "python313"
    entry_point       = "on_budget"
    service_account   = google_service_account.builder.id
    docker_repository = google_artifact_registry_repository.images["functions"].id
    source {
      storage_source {
        bucket = google_storage_bucket.function_source.name
        object = google_storage_bucket_object.billing_guard.name
      }
    }
  }

  service_config {
    max_instance_count    = 1
    available_memory      = "256M"
    timeout_seconds       = 60
    ingress_settings      = "ALLOW_INTERNAL_ONLY"
    service_account_email = google_service_account.billing_guard.email
    environment_variables = {
      PROJECT_ID       = var.project_id
      HARD_STOP_AMOUNT = tostring(var.hard_stop_amount)
      BUDGET_CURRENCY  = var.budget_currency
      DRY_RUN          = tostring(var.guard_dry_run)
    }
  }

  event_trigger {
    trigger_region        = var.region
    event_type            = "google.cloud.pubsub.topic.v1.messagePublished"
    pubsub_topic          = google_pubsub_topic.billing_alerts.id
    retry_policy          = "RETRY_POLICY_RETRY"
    service_account_email = google_service_account.billing_guard.email
  }

  depends_on = [google_project_iam_member.builder]
}

resource "google_cloud_run_service_iam_member" "billing_guard_invoker" {
  location = var.region
  service  = google_cloudfunctions2_function.billing_guard.name
  role     = "roles/run.invoker"
  member   = google_service_account.billing_guard.member
}
