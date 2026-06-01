# Module-level provider declaration: required only, NOT configured.
# Provider configuration (`provider "google" { project = ... }`) is the
# environment's responsibility — modules that configure providers can't
# be reused across accounts/projects. See infra/terraform/environments/dev/main.tf.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "= 5.45.0"
    }
  }
}
