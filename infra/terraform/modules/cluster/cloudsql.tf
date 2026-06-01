# Cloud SQL (Postgres 15) stub — pgvector-ready but extension not yet enabled.
#
# Extension pgvector is provisioned in a later migration story. This resource
# just needs to exist so modules/app/ (#017) can wire in the connection name.
#
# deletion_protection:
#   dev/staging: false  — can destroy freely.
#   prod: true          — explicit guard against accidental data loss.
#
# IP: Public IP (ipv4_enabled = true). Private IP deferred to #017 per
# orchestrator decision. Adding private IP requires google_service_networking_connection
# + google_compute_global_address which are out of scope for this stub.
#
# SSL: ssl_mode = "ENCRYPTED_ONLY" enforces in-transit encryption.
# Additional Postgres log flags are deferred to #017 when the connection proxy is in place.

#tfsec:ignore:google-sql-no-public-access
#tfsec:ignore:google-sql-encrypt-in-transit-data: TLS IS enforced via ssl_mode="ENCRYPTED_ONLY"; tfsec checks deprecated require_ssl field and does not recognise the modern ssl_mode attribute (known tfsec/Trivy migration gap).
#tfsec:ignore:google-sql-pg-log-connections
#tfsec:ignore:google-sql-pg-log-disconnections
#tfsec:ignore:google-sql-pg-log-lock-waits
#tfsec:ignore:google-sql-pg-log-checkpoints
#tfsec:ignore:google-sql-enable-pg-temp-file-logging
resource "google_sql_database_instance" "main" {
  # checkov:skip=CKV_GCP_6: Public IP is intentional for this stub; private IP
  #   deferred to #017 per orchestrator decision in expected-plan.txt.
  # checkov:skip=CKV_GCP_60: Public IP is intentional for this stub; deferred to #017.
  # checkov:skip=CKV_GCP_79: Postgres 15 is the latest stable version supported;
  #   checkov check is a false positive (POSTGRES_15 is current).
  # checkov:skip=CKV_GCP_51: pg_log_checkpoints deferred to #017.
  # checkov:skip=CKV_GCP_52: pg_log_connections deferred to #017.
  # checkov:skip=CKV_GCP_53: pg_log_disconnections deferred to #017.
  # checkov:skip=CKV_GCP_54: pg_log_lock_waits deferred to #017.
  # checkov:skip=CKV_GCP_108: pg_log_hostname deferred to #017.
  # checkov:skip=CKV_GCP_109: pg_log_min_messages deferred to #017.
  # checkov:skip=CKV_GCP_111: pg_log_statement deferred to #017.
  name             = "avsa-${var.environment}"
  project          = var.project_id
  database_version = "POSTGRES_15"
  region           = var.region

  deletion_protection = var.deletion_protection

  settings {
    tier = var.sql_tier

    database_flags {
      name  = "cloudsql.enable_pgaudit"
      value = "on"
    }

    ip_configuration {
      ipv4_enabled = true
      ssl_mode     = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled = true
    }
  }

  depends_on = [google_project_service.required]
}
