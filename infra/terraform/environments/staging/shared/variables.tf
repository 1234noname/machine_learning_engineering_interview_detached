variable "project_id" {
  description = "GCP project ID for the staging environment. Supply via terraform.tfvars or TF_VAR_project_id."
  type        = string
}

variable "region" {
  description = "Default GCP region for the provider."
  type        = string
  default     = "us-central1"
}

variable "github_repository" {
  description = "GitHub repository (OWNER/REPO). Defaults to the canonical AVSA repo."
  type        = string
  default     = "erinversfeldcodes/avsa"
}

variable "github_repository_owner" {
  description = "GitHub organisation/user. Defaults to the canonical AVSA owner."
  type        = string
  default     = "erinversfeldcodes"
}

variable "enable_gpu_pool" {
  description = "When true, creates the GPU node pool in the cluster module. staging: false."
  type        = bool
  default     = false
}

variable "subnet_cidr" {
  description = "CIDR range for the VPC subnet. Example: 10.0.0.0/20."
  type        = string
  default     = "10.0.0.0/20"
}
