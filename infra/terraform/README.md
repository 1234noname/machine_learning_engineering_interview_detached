# `infra/terraform/`

AVSA's Terraform configuration. Two-axis layout:

- **Shared modules library** under `modules/` — reusable bodies (e.g. `modules/wif/` from #5; `modules/app/` lands in Track B).
- **Per-environment, per-lifecycle roots** under `environments/<env>/{shared,app}/` — thin wrappers that call the modules.

The `shared/` vs `app/` split inside each env separates **persistent auth infrastructure** (WIF — must stay up so workflows can authenticate) from **ephemeral application resources** (per-PR in dev; singleton in staging/prod). They have separate state files and independent destroy semantics.

## Layout

```
infra/terraform/
├── README.md                       (this file)
├── modules/
│   └── wif/                        (Workload Identity Federation — #5)
│       ├── providers.tf            required_providers only — no provider config
│       ├── variables.tf            module inputs
│       ├── project.tf              API enables
│       ├── wif.tf                  pool / provider / SA / binding
│       ├── outputs.tf              wif_provider_url, github_deployer_email
│       └── README.md               module reference
└── environments/
    ├── dev/
    │   ├── shared/                 persistent auth substrate (apply once)
    │   │   ├── main.tf             backend prefix=dev/shared; calls modules/wif
    │   │   ├── variables.tf, outputs.tf
    │   │   ├── backend.tfvars.example, terraform.tfvars.example
    │   │   └── README.md
    │   └── app/                    ephemeral per-PR app stack (Track B+ populates)
    │       └── README.md           stub
    ├── staging/
    │   ├── shared/                 stub (populated when staging project exists)
    │   └── app/                    stub (populated by #9)
    └── prod/
        ├── shared/                 stub (populated when prod project exists)
        └── app/                    stub (populated by #10)
```

## Conventions

- **State buckets are per-project.** Naming: `<project-id>-terraform-state`.
- **State *prefixes* are per-environment-per-lifecycle.** `dev/shared`, `dev/app/pr-${N}` (where `${N}` is the PR number), `staging/shared`, `staging/app`, `prod/shared`, `prod/app`. Different prefixes = independent state files = independent destroy.
- **Backend config is partial.** `terraform { backend "gcs" {} }` in env code; `bucket` / `prefix` come from `-backend-config=backend.tfvars`. Same code points at different buckets per project.
- **Variables: tfvars files OR `TF_VAR_*` env vars** (both work; env vars compose with direnv + CI secrets).
- **GitHub Actions secrets are environment-scoped**, not repository-scoped: `gh secret set WIF_PROVIDER --env dev …`. Workflows declare `environment: dev/staging/prod` and read `secrets.WIF_PROVIDER` from the matching env.
- **Suppression comments are inline + documented.** `checkov:skip` and `tfsec:ignore` comments live next to the rule they bypass; the comment explains *why*.
- **Modules declare `required_providers`, NOT `provider {}` blocks.** Provider configuration is the env's responsibility — that's what makes the module reusable across accounts.

## State bucket bootstrap (one-time per project)

Required because Terraform itself can't bootstrap its own backend.

```bash
gcloud config set project <PROJECT_ID>
gsutil mb -l us-central1 gs://<PROJECT_ID>-terraform-state
gsutil versioning set on gs://<PROJECT_ID>-terraform-state
gsutil ubla set on gs://<PROJECT_ID>-terraform-state
gcloud storage buckets add-iam-policy-binding gs://<PROJECT_ID>-terraform-state \
    --member="user:$(gcloud config get-value account)" --role="roles/storage.admin"
```

Then `cd environments/<env>/shared/` for the auth bootstrap, then `cd environments/<env>/app/` once Track B has populated it for the app deploy.

(Full bootstrap — billing link, ADC quota, IAM grants — is scripted in [#5-B](../../issues/completed/005-B-scripted-gcp-setup.md). Once that lands: `just bootstrap-gcp <env> <project_id> <billing_id>`.)

## Verifier chain

Run from the repo root:

```bash
terraform -chdir=infra/terraform fmt -check -recursive
( cd infra/terraform/environments/dev/shared && terraform init -backend=false && terraform validate )
( cd infra/terraform && tflint --recursive )
tfsec infra/terraform
checkov -d infra/terraform --quiet --compact
```

All five exit 0 on a clean tree. CI for these lives in #2.

## Adding a new environment

1. `cp -r environments/dev environments/<new-env>` then trim/edit per the new env's needs.
2. Bootstrap the new env's GCP project + state bucket.
3. `cd environments/<new-env>/shared/ && terraform init -backend-config=backend.tfvars && terraform apply`.
4. App stack populates separately when there's an actual deploy (Track B+).

Module bodies under `modules/` don't change when adding an env — they're the contract; envs are call-sites.
