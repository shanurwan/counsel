# The Counsel Triage GitHub App (docs/triage-app.md). Its private key lives in its own
# secret, read only by the core runtime, which lists installations and posts triage
# notes to each installed repository's notes issue. Off until github_app_id is set:
# create the app, add the key as the secret's first version, then set the id.

variable "github_app_id" {
  type        = string
  default     = ""
  description = "The Counsel Triage app's numeric id, shown on its settings page. Empty keeps the app off."
}

resource "google_secret_manager_secret" "github_app_key" {
  secret_id = "github-app-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "runtime_github_app_key" {
  secret_id = google_secret_manager_secret.github_app_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.runtime.member
}
