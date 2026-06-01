output "app_url" {
  description = "URL where the deployed app is reachable via the GCE ingress load balancer."
  value       = try(data.kubernetes_ingress_v1.app.status[0].load_balancer[0].ingress[0].ip, "")
}

output "app_namespace" {
  description = "Kubernetes namespace the app deploys into."
  value       = helm_release.app.namespace
}
