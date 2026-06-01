# Secret Manager — name-only entries for AVSA secrets.
#
# IMPORTANT: No google_secret_manager_secret_version resources are created
# here. Secret values are inserted out-of-band by the operator. Terraform
# state contains NO secret values. This is intentional and required by spec.
#
# Secret naming convention: AVSA_<PURPOSE> maps to secret_id "AVSA_<PURPOSE>".
# These names are consumed by the External Secrets Operator on GKE.
#
# Replication uses automatic mode so GCP manages replica placement.

resource "google_secret_manager_secret" "db_password" {
  project   = var.project_id
  secret_id = "AVSA_DB_PASSWORD"

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret" "anthropic_api_key" {
  project   = var.project_id
  secret_id = "AVSA_ANTHROPIC_API_KEY"

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret" "storage_hmac_secret" {
  project   = var.project_id
  secret_id = "AVSA_STORAGE_HMAC_SECRET"

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret" "grafana_admin_password" {
  project   = var.project_id
  secret_id = "AVSA_GRAFANA_ADMIN_PASSWORD"

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}
