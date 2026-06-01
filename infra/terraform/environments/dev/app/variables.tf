variable "project_id" {
  description = "GCP project ID for the dev environment. Same as ../shared/'s project_id."
  type        = string
}

variable "region" {
  description = "Default GCP region for the provider."
  type        = string
  default     = "us-central1"
}

# Set by #8's PR workflow to pr-${{ github.event.number }} for ephemeral
# deploys; defaults empty for ad-hoc dev applies that don't need namespacing.
variable "name_suffix" {
  description = "Per-PR namespacing suffix. Set by the PR workflow; empty for ad-hoc applies."
  type        = string
  default     = ""
}

# Set by Track B's image-build step once that pipeline lands. Empty today
# (skeleton module ignores it).
variable "app_image" {
  description = "Container image tag built by Track B's pipeline. Empty until that lands."
  type        = string
  default     = ""
}

variable "artifact_registry_host" {
  description = "Artifact Registry hostname. Passed from dev GitHub Actions variable ARTIFACT_REGISTRY_HOST."
  type        = string
}

variable "cluster_endpoint" {
  description = "GKE cluster API server endpoint. Passed from dev GitHub Actions secret CLUSTER_ENDPOINT."
  type        = string
  sensitive   = true
}

variable "cluster_ca_certificate" {
  description = "Base64-encoded GKE cluster CA certificate. Passed from dev GitHub Actions secret CLUSTER_CA_CERTIFICATE."
  type        = string
  sensitive   = true
}
