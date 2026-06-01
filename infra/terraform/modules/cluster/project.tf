# Required GCP APIs for AVSA's cluster surface.
#
# disable_on_destroy=false because disabling APIs in a shared project can
# cascade into unrelated resources elsewhere; we'd rather leak an enabled
# API than break someone else's stack. Same convention as modules/wif/project.tf.

locals {
  required_apis = [
    "container.googleapis.com",
    "compute.googleapis.com",
    "sqladmin.googleapis.com",
    "artifactregistry.googleapis.com",
    "dns.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudbuild.googleapis.com",
  ]
}

resource "google_project_service" "required" {
  for_each = toset(local.required_apis)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}
