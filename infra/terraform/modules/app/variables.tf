variable "project_id" {
  description = "GCP project ID where the app deploys."
  type        = string
}

variable "region" {
  description = "Default GCP region for the provider configuration in the caller."
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Environment name: dev / staging / prod. Used in resource names and labels when Track B populates the module body. PR ephemerals use environment=dev + name_suffix=pr-N; 'pr' is not a valid environment value."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "name_suffix" {
  description = "Suffix concatenated into resource names for per-PR isolation. Empty for staging/prod singletons; pr-N for PR ephemerals."
  type        = string
  default     = ""
}

variable "app_image" {
  description = "Container image (registry/repo:tag) to deploy. Empty until Track B populates the build pipeline."
  type        = string
  default     = ""
}

variable "artifact_registry_host" {
  description = "Artifact Registry hostname (e.g. us-central1-docker.pkg.dev). Used to construct image repo URLs in the Helm release."
  type        = string
}

# tflint-ignore: terraform_unused_declarations
variable "cluster_endpoint" {
  description = "GKE cluster API server endpoint. Used to configure the Kubernetes and Helm providers in the calling environment."
  type        = string
  sensitive   = true
}

# tflint-ignore: terraform_unused_declarations
variable "cluster_ca_certificate" {
  description = "Base64-encoded GKE cluster CA certificate. Used to configure the Kubernetes and Helm providers in the calling environment."
  type        = string
  sensitive   = true
}
