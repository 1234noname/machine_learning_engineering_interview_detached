output "wif_provider_url" {
  description = "Full resource URL of the WIF provider. The environment re-exports this as the GitHub Actions secret WIF_PROVIDER."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "github_deployer_email" {
  description = "Deployer service account email. The environment re-exports this as the GitHub Actions secret WIF_SERVICE_ACCOUNT."
  value       = google_service_account.github_deployer.email
}
