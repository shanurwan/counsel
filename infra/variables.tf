variable "project_id" {
  type    = string
  default = "counsel-513516"
}

variable "billing_account" {
  type        = string
  description = "Billing account ID, e.g. 0123AB-4567CD-89EF01."
}

# us-central1: Cloud Run's free egress (1 GB/month) only applies to traffic
# leaving North America, and Cloud Storage's free tier only to US regions.
variable "region" {
  type    = string
  default = "us-central1"
}

variable "budget_currency" {
  type        = string
  default     = "MYR"
  description = "Must match the billing account's currency."
}

variable "hard_stop_amount" {
  type        = number
  default     = 4
  description = "Actual project cost (budget currency) at which billing is unlinked. RM 4 is about USD 1."
}

variable "guard_dry_run" {
  type        = bool
  default     = false
  description = "Log instead of unlinking billing. Only for testing the stop path."
}
