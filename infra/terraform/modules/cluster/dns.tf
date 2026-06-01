# Cloud DNS — private managed zone scoped to the cluster VPC.
#
# No DNS records are created here. Issue #017 adds the A record for the
# ingress load balancer once the service is deployed.
#
# The zone is private (visibility = "private") and bound to the VPC so
# only resources within the VPC can resolve the zone's records.

resource "google_dns_managed_zone" "zone" {
  project    = var.project_id
  name       = "avsa-${var.environment}"
  dns_name   = "avsa-${var.environment}.internal."
  visibility = "private"

  private_visibility_config {
    networks {
      network_url = google_compute_network.vpc.self_link
    }
  }

  depends_on = [google_project_service.required]
}
