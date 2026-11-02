terraform {
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.8"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.6"
    }
  }

  # Created once by scripts/bootstrap-state.sh (versioned, us-central1).
  backend "gcs" {
    bucket = "counsel-513516-tfstate"
    prefix = "counsel"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region

  # Budgets and billing calls are charged to this project's quota, not to
  # whatever project gcloud last pointed at.
  user_project_override = true
  billing_project       = var.project_id
}
