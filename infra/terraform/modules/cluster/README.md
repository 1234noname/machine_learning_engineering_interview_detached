# modules/cluster

Terraform module that provisions the persistent GCP infrastructure for AVSA:

- VPC + subnet + Cloud Router + Cloud NAT
- GKE cluster (Workload Identity, REGULAR release channel)
- CPU node pool (e2-standard-4, autoscaling min=1 max=3)
- GPU node pool (n1-standard-8 + NVIDIA T4, preemptible, autoscaling min=0 max=1) — conditional
- Artifact Registry (Docker format, repository ID `avsa`)
- Cloud SQL (Postgres 15, pgaudit enabled)
- Secret Manager secrets (name-only; values inserted out-of-band)
- Cloud DNS private zone (no records adds the ingress A record)
- IAM bindings for the WIF deployer SA

## Inputs

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `project_id` | `string` | — | GCP project ID where all resources are created. |
| `region` | `string` | `"us-central1"` | GCP region for all regional resources. |
| `environment` | `string` | — | Logical environment name: `dev`, `staging`, or `prod`. Used in resource names. |
| `enable_gpu_pool` | `bool` | `false` | When `true`, creates the GPU node pool. Set `false` for dev/staging, `true` for prod. |
| `deployer_sa_email` | `string` | — | Email of the WIF deployer SA. Sourced from `module.wif.github_deployer_email`. |
| `subnet_cidr` | `string` | — | CIDR range for the VPC subnet. Example: `10.0.0.0/20`. |
| `sql_tier` | `string` | `"db-f1-micro"` | Cloud SQL instance tier. Use `db-f1-micro` for dev/staging, `db-g1-small` for prod. |
| `deletion_protection` | `bool` | `false` | Enables deletion protection on Cloud SQL. Set `false` for dev/staging, `true` for prod. |

## Outputs

| Name | Description |
|------|-------------|
| `cluster_name` | GKE cluster name. Passed to `modules/app/` in. |
| `cluster_endpoint` | GKE control-plane endpoint (sensitive). Passed to `modules/app/` in. |
| `artifact_registry_host` | Docker registry hostname (`<region>-docker.pkg.dev`). Used by CI/CD deploy workflow. |
| `db_connection_name` | Cloud SQL instance connection name. Passed to `modules/app/` in. |
| `network_name` | VPC network name. Re-exported by environment `shared/outputs.tf`. |
| `subnet_name` | VPC subnet name. Re-exported by environment `shared/outputs.tf`. |

## Cost estimates (us-central1, on-demand, approximate)

| Resource | Spec | Estimated cost |
|----------|------|----------------|
| CPU node pool | e2-standard-4, min=1 max=3 | ~$100/month |
| GPU node pool | n1-standard-8 + T4, preemptible, min=0 max=1 | ~$0–$200/month (scales to zero) |
| Cloud SQL | db-f1-micro (dev/staging) | ~$10/month |
| Cloud SQL | db-g1-small (prod) | ~$25/month |
| Artifact Registry | storage only | <$5/month |

GPU node pool is the largest cost item. It is preemptible and scales to zero when no GPU workload runs.

## Tear-down path

To destroy the entire cluster stack for an environment:

```bash
terraform -chdir=infra/terraform/environments/<env>/shared destroy
```

To surgically remove only the GPU node pool (cost reduction without destroying the cluster):

```bash
terraform -chdir=infra/terraform/environments/prod/shared destroy \
  -target=module.cluster.google_container_node_pool.gpu_pool[0]
```

To protect prod resources from accidental destruction, add `prevent_destroy = true` lifecycle blocks to the cluster and SQL instance after the first successful prod apply (separate PR reviewer context).

## NVIDIA GPU driver installation

**Terraform does NOT install NVIDIA GPU drivers.** After creating a cluster with `enable_gpu_pool = true`, the operator must apply the NVIDIA driver DaemonSet:

```bash
kubectl apply -f https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/master/nvidia-driver-installer/cos/daemonset-preloaded.yaml
```

Wait for the DaemonSet pods to reach `Running` on all GPU nodes before scheduling GPU workloads:

```bash
kubectl -n kube-system rollout status daemonset/nvidia-driver-installer
```

## Usage

```hcl
module "cluster" {
  source = "../../../modules/cluster"

  project_id        = var.project_id
  region            = var.region
  environment       = "dev"
  enable_gpu_pool   = false
  deployer_sa_email = module.wif.github_deployer_email
  subnet_cidr       = var.subnet_cidr
}
```

For prod, override the SQL tier and deletion protection:

```hcl
module "cluster" {
  source = "../../../modules/cluster"

  project_id          = var.project_id
  region              = var.region
  environment         = "prod"
  enable_gpu_pool     = true
  deployer_sa_email   = module.wif.github_deployer_email
  subnet_cidr         = var.subnet_cidr
  sql_tier            = "db-g1-small"
  deletion_protection = true
}
```
