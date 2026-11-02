output "runtime_service_account" {
  value = google_service_account.runtime.email
}

output "image_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/counsel"
}

output "billing_guard" {
  value = google_cloudfunctions2_function.billing_guard.name
}
