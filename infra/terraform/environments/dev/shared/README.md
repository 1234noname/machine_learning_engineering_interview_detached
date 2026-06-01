# `environments/dev/shared/` — AVSA dev shared (persistent) infrastructure

The auth substrate every dev workflow rides on. Calls `../../../modules/wif/` to create the WIF pool, provider, deployer SA, and IAM binding. Apply this once; leave it up. PR-ephemeral and merge-driven workflows authenticate as the deployer SA from this stack.

State lives at `gs://<project-id>-terraform-state/dev/shared/`. Sibling: `../app/` — per-PR / per-deploy app stack (Track B+).

## One-time setup

```bash
cd infra/terraform/environments/dev/shared

cp backend.tfvars.example backend.tfvars
cp terraform.tfvars.example terraform.tfvars
$EDITOR backend.tfvars terraform.tfvars        # fill in real values

# Bucket bootstrap (if not done): see infra/terraform/README.md
#   gsutil mb -l us-central1 gs://<project-id>-terraform-state
#   gsutil versioning set on gs://<project-id>-terraform-state
#   gsutil ubla set on gs://<project-id>-terraform-state

terraform init -backend-config=backend.tfvars
```

## Plan / apply

```bash
terraform plan -out=plan.tfplan
# Diff against ../../../../../tests/terraform/wif/expected-plan.txt; material
# divergence (new resource types, removed resources, changed security guards) = stop.
terraform apply plan.tfplan
```

After apply, capture outputs and store as GitHub Actions environment-scoped secrets:

```bash
# Create the dev environment if it doesn't exist
gh api -X PUT repos/<owner>/<repo>/environments/dev

gh secret set WIF_PROVIDER        --env dev --body "$(terraform output -raw wif_provider_url)"
gh secret set WIF_SERVICE_ACCOUNT --env dev --body "$(terraform output -raw github_deployer_email)"
```

`--env dev` scopes the secrets to the GitHub `dev` Environment. Workflows that target dev (PR-ephemeral, merge → staging, prod gate, etc.) declare `environment: dev/staging/prod` and read `secrets.WIF_PROVIDER` from the matching env-scoped secret.

## Migrating from a single-root layout

If you previously ran `terraform apply` from `environments/dev/` (pre-#5-A) with state at `prefix=dev`, migrate state to `prefix=dev/shared`:

```bash
$EDITOR backend.tfvars   # change prefix to "dev/shared"
terraform init -migrate-state -reconfigure -backend-config=backend.tfvars
# Terraform asks "Do you want to copy existing state to the new backend?" — answer yes.
```

## Onboarding to a different GCP project

1. Update `backend.tfvars` (`bucket = <new-project-id>-terraform-state`).
2. Update `terraform.tfvars` (`project_id = ...`) — or `TF_VAR_project_id` env var.
3. Re-run `terraform init -reconfigure -backend-config=backend.tfvars`.

State is per-project (project-prefixed bucket); pointing the same code at two projects produces independent state, no cross-contamination.

## Tear-down

```bash
terraform destroy
```

**Caveat:** WIF pools and service accounts soft-delete for 30 days; the IDs (`github`, `github-deployer`) are reserved during that window. Re-applying within 30 days requires undelete + import (see `docs/runbooks/terraform.md` "Common failure modes"). Don't destroy casually.
