# AVSA prod — `shared/` Terraform root: persistent auth infrastructure.
#
# Calls `../../../modules/wif/`. State lives in a prod-project-specific GCS
# bucket at `prefix=prod/shared`. Once applied, this stack stays up.
#
# Apply discipline: the FIRST apply is necessarily manual (chicken-and-egg —
# #10's prod-deploy workflow uses the WIF SA from here to authenticate).
# After the first apply, prod is "online" and #10 takes over future applies.
# Manual local applies after that point are forbidden.
#
# Sibling: `../app/` — populated by #10's workflow (singleton; not per-PR).

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "= 5.45.0"
    }
  }

  # Partial backend configuration — the bucket / prefix are supplied via
  # `terraform init -backend-config=backend.tfvars`. See backend.tfvars.example.
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
  restrict_to_main        = true
}

module "cluster" {
  source = "../../../modules/cluster"

  project_id          = var.project_id
  region              = var.region
  environment         = "prod"
  enable_gpu_pool     = var.enable_gpu_pool
  deployer_sa_email   = module.wif.github_deployer_email
  subnet_cidr         = var.subnet_cidr
  sql_tier            = "db-g1-small"
  # deletion_protection = false for v1 submission: we can destroy and recreate
  # the Cloud SQL instance freely while iterating on the deploy. Set to true
  # only after the database holds irreplaceable production data.
  deletion_protection = false
}

# Deploy-events bucket — stores JSONL from emit-deploy-event.sh for DORA
# metric consumption by Track D. Separate from the Terraform state bucket
# (<project>-terraform-state). Public access prevention is enforced so a
# future IAM grant cannot accidentally expose historical deploy records.
resource "google_storage_bucket" "deploy_events" {
  # checkov:skip=CKV_GCP_78: soft-delete is redundant with object versioning;
  #   versioning below provides an equivalent recovery window.
  name                        = "${var.project_id}-deploy-events"
  project                     = var.project_id
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  # Retain event objects for 365 days; DORA metrics span at most the last year.
  lifecycle_rule {
    condition {
      age = 365
    }
    action {
      type = "Delete"
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Grant dev and staging deployer SAs write access so their deploy workflows
# can emit DORA events into this bucket. The bucket lives in prod but events
# come from all environments. objectCreator is the minimum required role.
resource "google_storage_bucket_iam_member" "deploy_events_writer_dev" {
  count  = var.dev_deployer_sa_email != "" ? 1 : 0
  bucket = google_storage_bucket.deploy_events.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${var.dev_deployer_sa_email}"
}

resource "google_storage_bucket_iam_member" "deploy_events_writer_staging" {
  count  = var.staging_deployer_sa_email != "" ? 1 : 0
  bucket = google_storage_bucket.deploy_events.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${var.staging_deployer_sa_email}"
}
