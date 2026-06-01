# Required GCP APIs for AVSA's WIF surface.
#
# disable_on_destroy=false because disabling APIs in a shared project can
# cascade into unrelated resources elsewhere; we'd rather leak an enabled
# API than break someone else's stack.

locals {
  required_apis = [
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "cloudresourcemanager.googleapis.com",
  ]
}

resource "google_project_service" "required" {
  for_each = toset(local.required_apis)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}
