variable "project_id" {
  description = "GCP project ID for the prod environment. MUST be a different project from dev/staging — strict env isolation. Supply via terraform.tfvars or TF_VAR_project_id."
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
  description = "When true, creates the GPU node pool in the cluster module. prod: true."
  type        = bool
  default     = true
}

variable "subnet_cidr" {
  description = "CIDR range for the VPC subnet. Example: 10.0.0.0/20."
  type        = string
  default     = "10.0.0.0/20"
}

variable "dev_deployer_sa_email" {
  description = "Email of the GitHub Actions deployer SA in the dev project. Granted objectCreator on the deploy-events bucket. Format: github-deployer@<dev-project>.iam.gserviceaccount.com. Leave empty to skip the IAM binding (safe for initial prod bootstrap before dev/staging exist)."
  type        = string
  default     = ""
}

variable "staging_deployer_sa_email" {
  description = "Email of the GitHub Actions deployer SA in the staging project. Granted objectCreator on the deploy-events bucket. Format: github-deployer@<staging-project>.iam.gserviceaccount.com. Leave empty to skip the IAM binding (safe for initial prod bootstrap before dev/staging exist)."
  type        = string
  default     = ""
}
