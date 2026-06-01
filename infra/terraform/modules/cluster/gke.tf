# GKE cluster, CPU node pool, and optional GPU node pool.
#
# Security notes:
#   - Workload Identity is enabled at the cluster level (workload_pool).
#     Per-pod SA bindings happen in modules/app/ (#017).
#   - The default node pool is removed immediately after cluster creation
#     (remove_default_node_pool = true). Both pools are managed node pools
#     so we retain full control over their lifecycle.
#   - NVIDIA GPU drivers are NOT managed by Terraform. The operator must
#     apply the NVIDIA driver DaemonSet after cluster creation:
#       kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/master/nvidia-driver-installer/cos/daemonset-preloaded.yaml
#     See README.md for full instructions.

# ---------------------------------------------------------------------------
# GKE Cluster
# ---------------------------------------------------------------------------
#tfsec:ignore:google-gke-enforce-pod-security-policy
#tfsec:ignore:google-gke-enable-master-networks
#tfsec:ignore:google-gke-enable-private-cluster
#tfsec:ignore:google-gke-metadata-endpoints-disabled
#tfsec:ignore:google-gke-enable-network-policy
#tfsec:ignore:google-gke-enable-ip-aliasing
#tfsec:ignore:google-gke-use-cluster-labels
resource "google_container_cluster" "cluster" {
  # checkov:skip=CKV_GCP_25: Private cluster deferred to hardening story; not required for stub.
  # checkov:skip=CKV_GCP_23: Alias IP ranges (VPC-native) deferred to networking hardening story.
  # checkov:skip=CKV_GCP_20: Master authorised networks deferred to hardening story.
  # checkov:skip=CKV_GCP_21: Resource labels deferred to tagging story.
  # checkov:skip=CKV_GCP_13: client_certificate_config is disabled by default in GKE 1.12+.
  # checkov:skip=CKV_GCP_65: Google Groups RBAC deferred to identity hardening story.
  # checkov:skip=CKV_GCP_64: Private nodes deferred to networking hardening story.
  # checkov:skip=CKV_GCP_66: Binary Authorization deferred to hardening story.
  # checkov:skip=CKV_GCP_69: GKE_METADATA mode is set on node pools; cluster-level check is a false positive.
  # checkov:skip=CKV_GCP_12: Network policy deferred to hardening story.
  # checkov:skip=CKV_GCP_61: Intranode visibility deferred to observability story.
  name     = "avsa-${var.environment}"
  project  = var.project_id
  location = var.region

  remove_default_node_pool = true
  initial_node_count       = 1

  network    = google_compute_network.vpc.self_link
  subnetwork = google_compute_subnetwork.subnet.self_link

  release_channel {
    channel = "REGULAR"
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  depends_on = [google_project_service.required]
}

# ---------------------------------------------------------------------------
# CPU node pool — always created
# ---------------------------------------------------------------------------
#tfsec:ignore:google-gke-use-service-account
#tfsec:ignore:google-gke-node-pool-uses-cos
resource "google_container_node_pool" "cpu_pool" {
  # checkov:skip=CKV_GCP_68: Shielded GKE nodes (Secure Boot) deferred to hardening story.
  name     = "cpu-pool"
  project  = var.project_id
  location = var.region
  cluster  = google_container_cluster.cluster.name

  autoscaling {
    min_node_count = 1
    max_node_count = 3
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  upgrade_settings {
    max_surge       = 1
    max_unavailable = 0
  }

  lifecycle {
    ignore_changes = [
      # GCP automatically adds resource_labels like
      # "goog-gke-node-pool-provisioning-model" that Terraform cannot
      # remove via UpdateNodePool (the API rejects resource_labels-only updates).
      node_config[0].resource_labels,
    ]
  }

  #tfsec:ignore:google-gke-metadata-endpoints-disabled
  node_config {
    machine_type = "e2-standard-4"

    # Workload Identity: use GKE metadata server so pods can obtain GCP
    # credentials without a long-lived key mounted into the container.
    # disable-legacy-endpoints=true disables the v0.1 and v1beta1 metadata
    # endpoints; only the v1 endpoint (with required Metadata-Flavor header)
    # is accessible.
    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    metadata = {
      disable-legacy-endpoints = "true"
    }

    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }
}

# ---------------------------------------------------------------------------
# GPU node pool — conditional on var.enable_gpu_pool
#
# Cost controls:
#   - preemptible = true  (heavily discounted; suitable for batch GPU work)
#   - min_node_count = 0  (scale-to-zero when no GPU workload runs)
#
# GPU taint ensures only GPU-tolerating pods land on this pool:
#   nvidia.com/gpu=present:NoSchedule
#
# IMPORTANT: After cluster creation, apply the NVIDIA driver DaemonSet.
# See README.md for the kubectl command.
# ---------------------------------------------------------------------------
#tfsec:ignore:google-gke-use-service-account
#tfsec:ignore:google-gke-node-pool-uses-cos
resource "google_container_node_pool" "gpu_pool" {
  # checkov:skip=CKV_GCP_68: Shielded GKE nodes (Secure Boot) deferred to hardening story.
  count = var.enable_gpu_pool ? 1 : 0

  name     = "gpu-pool"
  project  = var.project_id
  location = var.region
  cluster  = google_container_cluster.cluster.name

  # Single zone: L4 quota = 1 globally; us-central1-b had a successful STAGING
  # event (a+c had STOCKOUT). Single zone = exactly 1 node for min_node_count=1.
  node_locations = ["us-central1-b"]

  autoscaling {
    min_node_count = 1
    max_node_count = 1
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  upgrade_settings {
    max_surge       = 1
    max_unavailable = 0
  }

  lifecycle {
    ignore_changes = [
      node_config[0].resource_labels,
    ]
  }

  #tfsec:ignore:google-gke-metadata-endpoints-disabled
  node_config {
    # Switched from n1-standard-8 + T4 to g2-standard-4 + L4:
    #   - T4 zone capacity exhausted in all us-central1 zones (GCE out of resources)
    #   - L4 available in us-central1-a/b/c, NVIDIA_L4_GPUS quota = 1
    #   - L4 is a newer GPU (Ada Lovelace) with higher throughput than T4
    #   - g2-standard-4 uses standard CPUS quota (4 vCPUs; ample headroom at 200 limit)
    machine_type = "g2-standard-4"
    preemptible  = false

    guest_accelerator {
      type  = "nvidia-l4"
      count = 1
    }

    taint {
      key    = "nvidia.com/gpu"
      value  = "present"
      effect = "NO_SCHEDULE"
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    metadata = {
      disable-legacy-endpoints = "true"
    }

    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }
}
