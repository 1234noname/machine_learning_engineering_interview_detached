output "cluster_name" {
  description = "Name of the GKE cluster. Consumed by environments/*/shared/outputs.tf and modules/app/ in #017."
  value       = google_container_cluster.cluster.name
}

output "cluster_endpoint" {
  description = "GKE cluster control-plane endpoint. Consumed by environments/*/shared/outputs.tf and modules/app/ in #017."
  value       = google_container_cluster.cluster.endpoint
  sensitive   = true
}

output "artifact_registry_host" {
  description = "Docker registry hostname for Artifact Registry. Used by the CI/CD deploy workflow to push and pull images."
  value       = "${var.region}-docker.pkg.dev"
}

output "db_connection_name" {
  description = "Cloud SQL instance connection name. Consumed by environments/*/shared/outputs.tf and modules/app/ in #017."
  value       = google_sql_database_instance.main.connection_name
}

output "network_name" {
  description = "Name of the VPC network. Consumed by environments/*/shared/outputs.tf for internal wiring."
  value       = google_compute_network.vpc.name
}

output "subnet_name" {
  description = "Name of the VPC subnetwork. Consumed by environments/*/shared/outputs.tf for internal wiring."
  value       = google_compute_subnetwork.subnet.name
}

output "cluster_ca_certificate" {
  description = "Base64-encoded cluster CA certificate. Consumed by modules/app/ Kubernetes and Helm providers."
  value       = google_container_cluster.cluster.master_auth[0].cluster_ca_certificate
  sensitive   = true
}
