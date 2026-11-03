variable "grafana_url" {
  description = "Grafana Cloud stack URL, e.g. https://<stack>.grafana.net"
  type        = string
}

variable "grafana_token" {
  description = "Grafana service account token (Editor). Kept out of the repo."
  type        = string
  sensitive   = true
}

variable "db_host" {
  description = "Supabase pooler host:port, e.g. aws-0-eu-west-1.pooler.supabase.com:5432"
  type        = string
}

variable "db_user" {
  description = "counsel_reader as the pooler expects it: counsel_reader.<project-ref>"
  type        = string
}

variable "reader_password" {
  description = "Set with: alter role counsel_reader password '...'. Kept out of the repo."
  type        = string
  sensitive   = true
}

variable "alert_email" {
  description = "Where alerts go"
  type        = string
}

variable "monthly_budget_usd" {
  description = "Platform-wide monthly model spend before the budget alert fires"
  type        = number
  default     = 5
}
