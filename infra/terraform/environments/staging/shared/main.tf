# AVSA staging — `shared/` Terraform root: persistent auth infrastructure.
#
# Calls `../../../modules/wif/`. State lives in a project-specific GCS
# bucket at `prefix=staging/shared`. Once applied, this stack stays up —
# every staging workflow uses the WIF SA from here to authenticate.
# See docs/runbooks/release-pipeline.md § "Staging one-time setup" for the operator recipe.
#
# Sibling: `../app/` — the per-deploy app stack (Track B+).

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "= 5.45.0"
    }
  }

  # Partial backend configuration — the bucket / prefix are supplied via
  # `terraform init -backend-config=backend.tfvars` so we can point the same
  # code at different buckets per project. See backend.tfvars.example.
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}

module "wif" {
  source = "../../../modules/wif"

  project_id              = var.project_id
  github_repository       = var.github_repository
  github_repository_owner = var.github_repository_owner
}

module "cluster" {
  source = "../../../modules/cluster"

  project_id        = var.project_id
  region            = var.region
  environment       = "staging"
  enable_gpu_pool   = var.enable_gpu_pool
  deployer_sa_email = module.wif.github_deployer_email
  subnet_cidr       = var.subnet_cidr
}
