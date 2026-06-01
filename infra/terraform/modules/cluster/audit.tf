# Cloud Audit Logging — DATA_READ and DATA_WRITE for all services.
#
# GCP enables Admin Activity audit logs by default. DATA_READ and DATA_WRITE
# are off by default and must be explicitly enabled. Without them, a
# compromised deployer SA can read secrets, write to Cloud SQL, or enumerate
# GKE resources with no log trail.
#
# allServices coverage ensures new APIs enabled by this module are audited
# without requiring updates here each time a new service is added.

resource "google_project_iam_audit_config" "all_services" {
  project = var.project_id
  service = "allServices"

  audit_log_config {
    log_type = "ADMIN_READ"
  }

  audit_log_config {
    log_type = "DATA_READ"
  }

  audit_log_config {
    log_type = "DATA_WRITE"
  }

  depends_on = [google_project_service.required]
}
