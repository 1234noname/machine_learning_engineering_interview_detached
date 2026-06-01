# VPC, regional subnet, Cloud Router, and Cloud NAT for the AVSA cluster.
#
# Cloud NAT provides outbound internet access for nodes and pods without
# assigning public IPs to the nodes themselves. This is required for
# pulling container images from Docker Hub and other external registries.

# ---------------------------------------------------------------------------
# VPC
# ---------------------------------------------------------------------------
resource "google_compute_network" "vpc" {
  name                    = "avsa-${var.environment}"
  project                 = var.project_id
  auto_create_subnetworks = false

  depends_on = [google_project_service.required]
}

# ---------------------------------------------------------------------------
# Subnet
# VPC flow logs and private_ip_google_access deferred to observability/
# networking hardening stories.
# ---------------------------------------------------------------------------
#tfsec:ignore:google-compute-enable-vpc-flow-logs
resource "google_compute_subnetwork" "subnet" {
  # checkov:skip=CKV_GCP_26: VPC flow logs deferred to observability story.
  # checkov:skip=CKV_GCP_74: private_ip_google_access deferred to networking hardening story.
  name          = "avsa-${var.environment}"
  project       = var.project_id
  region        = var.region
  network       = google_compute_network.vpc.self_link
  ip_cidr_range = var.subnet_cidr
}

# ---------------------------------------------------------------------------
# Cloud Router (prerequisite for Cloud NAT)
# ---------------------------------------------------------------------------
resource "google_compute_router" "router" {
  name    = "avsa-${var.environment}"
  project = var.project_id
  region  = var.region
  network = google_compute_network.vpc.self_link
}

# ---------------------------------------------------------------------------
# Cloud NAT — outbound internet access for cluster nodes
# ---------------------------------------------------------------------------
resource "google_compute_router_nat" "nat" {
  name                               = "avsa-${var.environment}"
  project                            = var.project_id
  region                             = var.region
  router                             = google_compute_router.router.name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}
