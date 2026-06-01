# `modules/app/` — AVSA application module

**Status:** Active — deploys the AVSA api and model services via a Helm release on GKE.

## What it creates

- `helm_release.app` — Deploys the AVSA Helm chart (`helm/`) into a dedicated namespace on GKE.
- `data.kubernetes_ingress_v1.app` — Reads back the GCE ingress after the Helm release settles, to export the load-balancer IP as `app_url`.

The Helm release creates:
- A Kubernetes namespace (`avsa-<environment>[-<suffix>]`)
- API service deployment + service
- Model service deployment + service with GPU resource requests
- A GCE ingress backed by a static IP (`avsa-ingress` — see Prerequisites below)

## Provider requirements

This module declares `helm` (= 2.17.0) and `kubernetes` (= 2.37.1) as required providers. **It does NOT configure them** — provider configuration belongs in the calling environment (`environments/staging/app/` or `environments/prod/app/`), following the same pattern as `modules/wif/`.

The calling environment must configure the `helm` and `kubernetes` providers using:
- `host`: `"https://${var.cluster_endpoint}"`
- `cluster_ca_certificate`: `base64decode(var.cluster_ca_certificate)`
- `token`: `data.google_client_config.default.access_token`

## Prerequisites

**GCE static IP `avsa-ingress` must be pre-provisioned in GCP** before the Helm ingress resource can bind to it. This is an out-of-band step not managed by this module. To provision it:

```bash
gcloud compute addresses create avsa-ingress \
  --global \
  --project <PROJECT_ID>
```

The ingress annotation `kubernetes.io/ingress.global-static-ip-name: avsa-ingress` in `helm/values.gke.yaml` references this name.

## Inputs

| Variable | Type | Default | Description |
|---|---|---|---|
| `project_id` | string | (required) | GCP project ID |
| `region` | string | `us-central1` | Default GCP region for the provider configuration in the caller |
| `environment` | string | (required) | `dev` / `staging` / `prod` (validated) |
| `name_suffix` | string | `""` | Concatenated into resource names; `pr-N` for PR ephemerals; empty for staging/prod singletons |
| `app_image` | string | `""` | Container image tag to deploy (e.g. commit SHA). Empty string passes an empty tag to the Helm release — Terraform will error at apply time if no real tag is supplied. |
| `artifact_registry_host` | string | (required) | Artifact Registry hostname (e.g. `us-central1-docker.pkg.dev`). Used to construct image repo URLs in the Helm release |
| `cluster_endpoint` | string (sensitive) | (required) | GKE cluster API server endpoint. Used to configure the Kubernetes and Helm providers in the calling environment |
| `cluster_ca_certificate` | string (sensitive) | (required) | Base64-encoded GKE cluster CA certificate. Used to configure the Kubernetes and Helm providers in the calling environment |

## Outputs

| Output | Description |
|---|---|
| `app_url` | IP address of the GCE ingress load balancer. Sourced from `data.kubernetes_ingress_v1.app`. Empty string if the ingress has not yet assigned an IP. |
| `app_namespace` | Kubernetes namespace the app deploys into. Sourced from `helm_release.app.namespace`. |

## Usage

```hcl
provider "helm" {
  kubernetes {
    host                   = "https://${var.cluster_endpoint}"
    cluster_ca_certificate = base64decode(var.cluster_ca_certificate)
    token                  = data.google_client_config.default.access_token
  }
}

provider "kubernetes" {
  host                   = "https://${var.cluster_endpoint}"
  cluster_ca_certificate = base64decode(var.cluster_ca_certificate)
  token                  = data.google_client_config.default.access_token
}

data "google_client_config" "default" {}

module "app" {
  source = "../../../modules/app"

  project_id             = var.project_id
  region                 = var.region
  environment            = "staging"
  name_suffix            = var.name_suffix
  app_image              = var.app_image
  artifact_registry_host = var.artifact_registry_host
  cluster_endpoint       = var.cluster_endpoint
  cluster_ca_certificate = var.cluster_ca_certificate
}
```

See `infra/terraform/environments/staging/app/` for the canonical caller.

## Estimated cost

The app module itself adds no compute cost beyond what the GKE cluster provides. Costs are driven by:

- **GPU node pool** (from `modules/cluster/`): ~$0.35/hr per NVIDIA T4 node
- **GCE load balancer**: ~$0.025/hr for the forwarding rule + data egress

## Tear-down

```bash
terraform -chdir=infra/terraform/environments/staging/app destroy
```

This destroys the Helm release and all Kubernetes resources in the namespace. The GKE cluster and GCE static IP (`avsa-ingress`) are managed separately and are not affected. To release the static IP:

```bash
gcloud compute addresses delete avsa-ingress --global --project <PROJECT_ID>
```
