# Artifact Registry — Docker format repository for AVSA container images.
#
# Location matches var.region so the registry hostname matches the artifact_registry_host output.
# Repository ID is "avsa" as specified in the issue.

resource "google_artifact_registry_repository" "avsa" {
  # checkov:skip=CKV_GCP_84: Customer-Supplied Encryption Keys (CSEK) deferred to
  #   hardening story. Google-managed encryption is sufficient for this stub.
  project       = var.project_id
  location      = var.region
  repository_id = "avsa"
  format        = "DOCKER"
  description   = "AVSA container images"

  depends_on = [google_project_service.required]
}
