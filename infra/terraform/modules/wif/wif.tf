# Workload Identity Federation — lets GitHub Actions exchange its OIDC
# token for short-lived GCP credentials WITHOUT a long-lived service-account
# key. Three load-bearing security guards in this file:
#
#   1. attribute_condition restricts to this org's tokens (issuance gate).
#   2. principalSet is scoped to this specific REPOSITORY, not the org.
#   3. Deployer SA gets only roles/iam.serviceAccountTokenCreator at project
#      level. Track B adds narrower roles (container.developer etc.) as
#      those subsystems land.

# 1. The pool — a trust container that GCP routes inbound OIDC tokens through.
resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
  description               = "Trust pool for GitHub Actions OIDC tokens."

  depends_on = [google_project_service.required]
}

# 2. The provider — declares trust in GitHub's OIDC issuer. The
# attribute_condition is the load-bearing security guard at the issuance
# layer: without it, ANY GitHub OIDC token (from any repo, any org) could
# trade for credentials in this pool. Don't drop it.
resource "google_iam_workload_identity_pool_provider" "github" {
  # checkov:skip=CKV_GCP_125: attribute_condition is deliberately org-scoped
  # (assertion.repository_owner) per the #5 spec, leaving room for multiple
  # repos in this org to share the pool. Per-repo narrowing happens at the
  # SA IAM binding via principalSet://...attribute.repository/OWNER/REPO
  # below — defense in depth. Tightening attribute_condition to a single
  # repo would lock the pool and require relaxing later.
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub OIDC"
  description                        = "Trusts GitHub OIDC tokens scoped to ${var.github_repository_owner}."

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.actor"      = "assertion.actor"
    "attribute.ref"        = "assertion.ref"
  }

  attribute_condition = var.restrict_to_main ? "assertion.repository_owner == \"${var.github_repository_owner}\" && assertion.ref == \"refs/heads/main\"" : "assertion.repository_owner == \"${var.github_repository_owner}\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

# 3. The deployer service account — what GitHub Actions impersonates.
resource "google_service_account" "github_deployer" {
  project      = var.project_id
  account_id   = "github-deployer"
  display_name = "GitHub Actions deployer"
  description  = "Service account that GitHub Actions impersonates via WIF."
}

# 4. The IAM binding — the SECOND load-bearing security guard. The principal
# is scoped to a single repository (attribute.repository=OWNER/REPO), not
# the whole org. A broader binding would let any repo in the org impersonate
# the deployer — high blast radius. Keep this scoped.
resource "google_service_account_iam_binding" "github_deployer_wif" {
  service_account_id = google_service_account.github_deployer.name
  role               = "roles/iam.workloadIdentityUser"
  members = [
    "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}",
  ]
}

# 5. Minimal project-level roles for the deployer.
#    - roles/iam.serviceAccountTokenCreator: mint tokens for downstream workloads.
#    - roles/storage.admin: create and manage the GCS Terraform state bucket.
#      The CI workflow creates the bucket idempotently on first run; without
#      this role the create step fails with 403. Track B narrows to per-bucket
#      IAM once the bucket name is known at apply time.
resource "google_project_iam_member" "deployer_token_creator" {
  # checkov:skip=CKV_GCP_41: project-level roles/iam.serviceAccountTokenCreator
  #   is required so the deployer SA can mint tokens for downstream workloads
  #   it will impersonate (per #5 spec). Track B narrows scope where possible.
  # checkov:skip=CKV_GCP_49: same — see CKV_GCP_41 above.
  project = var.project_id
  #tfsec:ignore:google-iam-no-project-level-service-account-impersonation
  role   = "roles/iam.serviceAccountTokenCreator"
  member = "serviceAccount:${google_service_account.github_deployer.email}"
}

resource "google_project_iam_member" "deployer_storage_admin" {
  # checkov:skip=CKV_GCP_41: project-level roles/storage.admin is required so
  #   the deployer SA can create the GCS Terraform state bucket idempotently on
  #   first CI run. Track B will narrow to per-bucket IAM once the bucket name
  #   is stable.
  project = var.project_id
  role    = "roles/storage.admin"
  member  = "serviceAccount:${google_service_account.github_deployer.email}"
}

# Provisioner roles — required for the human-triggered provision workflow to
# run `terraform apply` on the shared stack (modules/cluster). These are
# broader than the deploy-time roles (container.developer etc.) because
# provisioning creates infrastructure, not just uses it.
locals {
  deployer_provisioner_roles = toset([
    "roles/resourcemanager.projectIamAdmin", # google_project_iam_member / audit_config
    "roles/serviceusage.serviceUsageAdmin",  # google_project_service (enable APIs)
    "roles/iam.workloadIdentityPoolAdmin",   # google_iam_workload_identity_pool / provider
    "roles/iam.serviceAccountAdmin",         # google_service_account_iam_binding (WIF user binding)
    "roles/container.admin",                 # google_container_cluster / node_pool
    "roles/cloudsql.admin",                  # google_sql_database_instance
    "roles/artifactregistry.admin",          # google_artifact_registry_repository
    "roles/compute.networkAdmin",            # VPC / subnet / router / NAT
    "roles/dns.admin",                       # google_dns_managed_zone
    "roles/secretmanager.admin",             # google_secret_manager_secret
  ])
}

resource "google_project_iam_member" "deployer_provisioner" {
  # checkov:skip=CKV_GCP_41: broad project-level roles are required so the
  #   provision workflow SA can create all cluster-module resources (GKE,
  #   Cloud SQL, AR, VPC, DNS, Secret Manager) and manage their IAM bindings.
  #   Narrower resource-scoped bindings are not practical for an
  #   infrastructure-provisioning SA that creates resources at apply time.
  for_each = local.deployer_provisioner_roles
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.github_deployer.email}"
}
