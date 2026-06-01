output "app_url" {
  description = "URL where the deployed prod app is reachable. null while the module is a skeleton; Track B populates."
  value       = module.app.app_url
}

output "app_namespace" {
  description = "Kubernetes namespace. null while the module is a skeleton; Track B populates."
  value       = module.app.app_namespace
}
