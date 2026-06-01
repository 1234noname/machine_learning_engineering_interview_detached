variable "project_id" {
  description = "GCP project ID for the prod environment."
  type        = string
}

variable "region" {
  description = "Default GCP region for the provider."
  type        = string
  default     = "us-central1"
}

variable "name_suffix" {
  description = "Suffix appended to resource names. Defaults to 'prod' for the singleton stack."
  type        = string
  default     = "prod"
}

variable "app_image" {
  description = "Container image tag built by Track B's pipeline. Empty until that lands."
  type        = string
  default     = ""
}

variable "artifact_registry_host" {
  description = "Artifact Registry hostname. Passed from prod GitHub Actions variable ARTIFACT_REGISTRY_HOST."
  type        = string
}

variable "cluster_endpoint" {
  description = "GKE cluster API server endpoint. Passed from prod GitHub Actions secret CLUSTER_ENDPOINT."
  type        = string
  sensitive   = true
}

variable "cluster_ca_certificate" {
  description = "Base64-encoded GKE cluster CA certificate. Passed from prod GitHub Actions secret CLUSTER_CA_CERTIFICATE."
  type        = string
  sensitive   = true
}
