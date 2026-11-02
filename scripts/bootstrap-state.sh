#!/usr/bin/env sh
# One-time: the versioned bucket that holds Terraform state (free tier: 5 GB in US regions).
set -eu
PROJECT="${1:-counsel-513516}"
gcloud storage buckets create "gs://${PROJECT}-tfstate" --project "$PROJECT" --location us-central1 --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update "gs://${PROJECT}-tfstate" --versioning
