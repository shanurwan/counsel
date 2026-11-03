# The cost dashboard and its alerts, on Grafana Cloud free. Grafana reads
# Supabase as counsel_reader (db/migrations/0006_reader.sql): the cost views
# only, read-only, never a prompt, ticket or secret. Postgres stays the source
# of truth; Grafana is a window on the same views `counsel report` queries.
# Separate root and state from ../ so a Grafana token never sits beside GCP.

terraform {
  required_version = ">= 1.9"

  required_providers {
    grafana = {
      source  = "grafana/grafana"
      version = "~> 4.0"
    }
  }

  backend "gcs" {
    bucket = "counsel-513516-tfstate"
    prefix = "grafana"
  }
}

provider "grafana" {
  url  = var.grafana_url
  auth = var.grafana_token
}

resource "grafana_folder" "counsel" {
  title = "Counsel"
}

resource "grafana_data_source" "counsel" {
  type     = "grafana-postgresql-datasource"
  name     = "counsel (Supabase, read-only)"
  url      = var.db_host
  username = var.db_user

  json_data_encoded = jsonencode({
    database        = "postgres"
    sslmode         = "require"
    postgresVersion = 1700
    maxOpenConns    = 2
    maxIdleConns    = 1
    connMaxLifetime = 14400
  })
  secure_json_data_encoded = jsonencode({
    password = var.reader_password
  })
}

resource "grafana_dashboard" "cost" {
  folder      = grafana_folder.counsel.uid
  config_json = replace(file("${path.module}/dashboard.json"), "COUNSEL_DS_UID", grafana_data_source.counsel.uid)
}

resource "grafana_contact_point" "owner" {
  name = "counsel-owner"

  email {
    addresses = [var.alert_email]
  }
}

locals {
  # Each query returns one number; the rule fires when it is above the threshold.
  alerts = {
    "Agent run refused by Postgres" = {
      severity  = "security"
      sql       = "select count(*)::float as value from counsel.run_costs where status = 'denied' and started_at > now() - interval '35 minutes'"
      threshold = 0
      summary   = "An agent asked for something its role does not have and Postgres refused. Nothing was exposed. Run `counsel replay <run>` to read the instruction that tried it."
    }
    "Projected monthly spend over budget" = {
      severity  = "cost"
      sql       = "select coalesce(sum(usd), 0)::float * 30 as value from counsel.run_costs where started_at > now() - interval '24 hours'"
      threshold = var.monthly_budget_usd
      summary   = "The last 24 hours, run for a month, exceed the platform budget. `counsel report` shows which agent and why."
    }
    "Billed model calls produced nothing" = {
      severity  = "cost"
      sql       = "select count(*)::float as value from counsel.call_costs where error is not null and usd > 0 and created_at > now() - interval '24 hours'"
      threshold = 0
      summary   = "A model answer was cut off at max_tokens: billed, discarded, and retried with the same limit. Raise max_tokens for that call."
    }
    "Runs failed every attempt" = {
      severity  = "reliability"
      sql       = "select count(*)::float as value from counsel.run_costs where status = 'failed' and started_at > now() - interval '24 hours'"
      threshold = 0
      summary   = "A job used all its attempts and was dead-lettered. `counsel replay <run>` shows where it stopped."
    }
  }
}

resource "grafana_rule_group" "counsel" {
  name             = "counsel"
  folder_uid       = grafana_folder.counsel.uid
  interval_seconds = 300

  dynamic "rule" {
    for_each = local.alerts
    content {
      name           = rule.key
      condition      = "B"
      for            = "0s"
      no_data_state  = "OK"
      exec_err_state = "Error"
      labels         = { severity = rule.value.severity }
      annotations    = { summary = rule.value.summary }

      notification_settings {
        contact_point = grafana_contact_point.owner.name
      }

      data {
        ref_id         = "A"
        datasource_uid = grafana_data_source.counsel.uid
        relative_time_range {
          from = 86400
          to   = 0
        }
        model = jsonencode({
          refId      = "A"
          format     = "table"
          rawQuery   = true
          editorMode = "code"
          rawSql     = rule.value.sql
        })
      }

      data {
        ref_id         = "B"
        datasource_uid = "__expr__"
        relative_time_range {
          from = 0
          to   = 0
        }
        model = jsonencode({
          refId      = "B"
          type       = "threshold"
          expression = "A"
          conditions = [{ evaluator = { type = "gt", params = [rule.value.threshold] } }]
        })
      }
    }
  }
}

output "dashboard_url" {
  value = grafana_dashboard.cost.url
}
