variable "project_id" {
  description = "GCP project ID where WIF resources live."
  type        = string
}

variable "github_repository" {
  description = "GitHub repository in OWNER/REPO format. The WIF binding's principalSet is scoped to this exact repository."
  type        = string
}

variable "github_repository_owner" {
  description = "GitHub organisation/user that owns the repository. Used in the provider's attribute_condition to gate token issuance."
  type        = string
}

variable "restrict_to_main" {
  description = "When true, AND assertion.ref == 'refs/heads/main' onto the attribute_condition. Prevents non-main-branch tokens from obtaining credentials — required for prod."
  type        = bool
  default     = false
}
