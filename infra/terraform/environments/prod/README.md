# `environments/prod/` — AVSA production infrastructure

The prod environment is split into two Terraform roots:

| Root | Purpose | State prefix |
|---|---|---|
| [`shared/`](shared/) | Persistent infrastructure: GKE cluster, GPU node pool, Cloud SQL, Artifact Registry, Secret Manager, WIF pool + provider + deployer SA, DORA events bucket | `prod/shared` |
| [`app/`](app/) | Singleton app stack: Kubernetes workloads deployed via the `modules/app/` module. Driven by `prod-deploy.yml` after `shared/` is online. | `prod/app` |

State lives in a GCS bucket named `<project-id>-terraform-state` (created out-of-band by `just deploy-prod-manual` or `just bootstrap-gcp`).

## Inputs

### `shared/` required variables

| Variable | Description | Default |
|---|---|---|
| `project_id` | GCP project ID (MUST be separate from dev/staging) | — |
| `region` | GCP region | `us-central1` |
| `dev_deployer_sa_email` | Dev deployer SA email for DORA bucket write access | — |
| `staging_deployer_sa_email` | Staging deployer SA email for DORA bucket write access | — |
| `enable_gpu_pool` | Create the GPU node pool | `true` |
| `subnet_cidr` | VPC subnet CIDR | `10.0.0.0/20` |
| `github_repository` | `OWNER/REPO` for WIF binding | `erinversfeldcodes/avsa` |
| `github_repository_owner` | GitHub org/user | `erinversfeldcodes` |

### `app/` required variables

| Variable | Description | Default |
|---|---|---|
| `project_id` | GCP project ID | — |
| `artifact_registry_host` | Artifact Registry hostname | — |
| `cluster_endpoint` | GKE cluster API server endpoint | — |
| `cluster_ca_certificate` | Base64-encoded cluster CA certificate | — |

## Outputs (`shared/`)

| Output | Description |
|---|---|
| `wif_provider_url` | WIF provider resource URL — set as GitHub secret `WIF_PROVIDER` on the `prod` environment |
| `github_deployer_email` | Deployer SA email — set as `WIF_SERVICE_ACCOUNT` |
| `cluster_name` | GKE cluster name |
| `cluster_endpoint` | GKE control-plane endpoint (sensitive) |
| `artifact_registry_host` | Docker registry hostname |
| `db_connection_name` | Cloud SQL connection name |

## First-deploy procedure

The first apply is manual (chicken-and-egg: the prod-deploy workflow authenticates via the WIF SA that `shared/` creates):

```bash
# 1. Ensure ADC is configured
gcloud auth application-default login

# 2. Set the project ID
export GCP_PROJECT_ID=avsa-prd

# 3. Run the manual first-deploy script
just deploy-prod-manual
```

See `scripts/deploy-prod.sh` and `docs/runbooks/release-pipeline.md` for the full procedure.

## Cost estimate

See [`COSTS.md`](COSTS.md) for a back-of-envelope breakdown.

## Tear-down

```bash
# Destroy app stack first (workloads depend on cluster)
cd app && terraform init -backend-config=backend.tfvars && terraform destroy

# Destroy shared stack (cluster, Cloud SQL, etc.)
cd shared && terraform init -backend-config=backend.tfvars && terraform destroy
```

**Caveats:**
- Cloud SQL `deletion_protection = false` is set for v1 to allow clean tear-down. Change to `true` before storing irreplaceable data.
- WIF pool and service account soft-delete for 30 days; their IDs (`github`, `github-deployer`) are reserved during that window. See `docs/runbooks/terraform.md` for the undelete procedure.
- The DORA events bucket has `prevent_destroy = true` to protect historical deploy records. Remove the lifecycle block before running `terraform destroy` if needed.
