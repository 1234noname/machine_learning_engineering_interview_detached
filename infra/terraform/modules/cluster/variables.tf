variable "project_id" {
  description = "GCP project ID where all cluster resources will be created."
  type        = string
}

variable "region" {
  description = "Default GCP region for all resources."
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Logical environment name used in resource naming. Valid values: dev, staging, prod."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "enable_gpu_pool" {
  description = "When true, creates the GPU node pool (n1-standard-8 + NVIDIA T4, preemptible, min=0 max=1). Set false for dev/staging, true for prod."
  type        = bool
  default     = false
}

variable "deployer_sa_email" {
  description = "Email of the WIF deployer service account. Receives container.developer, artifactregistry.writer, and cloudsql.client project IAM roles."
  type        = string
}

variable "subnet_cidr" {
  description = "CIDR range for the VPC subnetwork. Must not overlap with other subnets in the project. Example: 10.0.0.0/20."
  type        = string
}

variable "sql_tier" {
  description = "Cloud SQL instance tier. Use db-f1-micro for dev/staging, db-g1-small for prod."
  type        = string
  default     = "db-f1-micro"
}

variable "deletion_protection" {
  description = "Whether to enable deletion protection on the Cloud SQL instance. Set false for dev/staging, true for prod."
  type        = bool
  default     = false
}
