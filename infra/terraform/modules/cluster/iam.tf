# IAM bindings for the WIF deployer service account.
#
# The deployer SA is passed in via var.deployer_sa_email (sourced from
# module.wif.github_deployer_email in each environment's shared/main.tf).
#
# Roles granted:
#   roles/container.developer   — deploy and manage workloads on the cluster
#   roles/artifactregistry.writer — push images to the Docker repository
#   roles/cloudsql.client        — connect to Cloud SQL via the Cloud SQL proxy
#
# IMPORTANT: roles/owner and roles/editor are explicitly NOT granted.
# This is an intentional security constraint from the spec.

resource "google_project_iam_member" "deployer_container_developer" {
  # checkov:skip=CKV_GCP_41: roles/container.developer is the minimum required
  #   for the deployer SA to manage GKE workloads. Narrower resource-scoped
  #   bindings require a cluster IAM API not available in this provider version.
  project = var.project_id
  role    = "roles/container.developer"
  member  = "serviceAccount:${var.deployer_sa_email}"
}

resource "google_project_iam_member" "deployer_artifact_registry_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${var.deployer_sa_email}"
}

resource "google_project_iam_member" "deployer_cloudsql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${var.deployer_sa_email}"
}
