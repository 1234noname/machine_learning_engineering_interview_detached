# AVSA prod — `app/` Terraform root: singleton prod app stack.
# Calls `../../../modules/app/`.
#
# State at gs://avsa-prod-terraform-state/prod/app via the partial
# backend config (bucket/prefix supplied via -backend-config flags in CI).
#
# Sibling: ../shared/ — persistent auth substrate.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "= 5.45.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "= 2.17.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "= 2.37.1"
    }
  }

  # Partial backend — bucket/prefix supplied via -backend-config flags in CI.
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "helm" {
  kubernetes {
    host                   = "https://${var.cluster_endpoint}"
    cluster_ca_certificate = base64decode(var.cluster_ca_certificate)
    token                  = data.google_client_config.default.access_token
  }
}

provider "kubernetes" {
  host                   = "https://${var.cluster_endpoint}"
  cluster_ca_certificate = base64decode(var.cluster_ca_certificate)
  token                  = data.google_client_config.default.access_token
}

data "google_client_config" "default" {}

module "app" {
  source = "../../../modules/app"

  project_id             = var.project_id
  region                 = var.region
  environment            = "prod"
  name_suffix            = var.name_suffix
  app_image              = var.app_image
  artifact_registry_host = var.artifact_registry_host
  cluster_endpoint       = var.cluster_endpoint
  cluster_ca_certificate = var.cluster_ca_certificate
}
