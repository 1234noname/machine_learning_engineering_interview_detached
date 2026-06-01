output "wif_provider_url" {
  description = "Full resource URL of the WIF provider. Set as GitHub Actions repository secret WIF_PROVIDER."
  value       = module.wif.wif_provider_url
}

output "github_deployer_email" {
  description = "Deployer SA email. Set as GitHub Actions repository secret WIF_SERVICE_ACCOUNT."
  value       = module.wif.github_deployer_email
}

output "cluster_name" {
  description = "GKE cluster name. Consumed by ../app/ in #017."
  value       = module.cluster.cluster_name
}

output "cluster_endpoint" {
  description = "GKE cluster control-plane endpoint. Consumed by ../app/ in #017."
  value       = module.cluster.cluster_endpoint
  sensitive   = true
}

output "artifact_registry_host" {
  description = "Docker registry hostname for Artifact Registry. Used by CI/CD deploy workflow."
  value       = module.cluster.artifact_registry_host
}

output "db_connection_name" {
  description = "Cloud SQL instance connection name. Consumed by ../app/ in #017."
  value       = module.cluster.db_connection_name
}

output "network_name" {
  description = "VPC network name."
  value       = module.cluster.network_name
}

output "subnet_name" {
  description = "VPC subnet name."
  value       = module.cluster.subnet_name
}

output "cluster_ca_certificate" {
  description = "Base64-encoded GKE cluster CA certificate. Passed to environments/staging/app/ for Kubernetes/Helm provider config."
  value       = module.cluster.cluster_ca_certificate
  sensitive   = true
}
