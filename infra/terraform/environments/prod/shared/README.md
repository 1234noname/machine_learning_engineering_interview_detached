# `environments/prod/shared/` — AVSA prod shared (persistent) infrastructure

The auth substrate for prod workflows. Calls `../../../modules/wif/` against the prod GCP project.

## One-time setup (manual chicken-and-egg)

Prod's first apply is necessarily manual — #10's prod-deploy workflow uses the WIF SA created here to authenticate, so the SA must exist before the workflow can run.

```bash
# 1. Create the prod GCP project (manual; not scripted — project IDs are precious).
gcloud projects create <prod-project-id> --name="AVSA Prod"

# 2. Bootstrap (idempotent script from #5-B):
just bootstrap-gcp prod <prod-project-id> <billing-account-id>

# 3. Configure the env:
cd infra/terraform/environments/prod/shared
cp backend.tfvars.example backend.tfvars
cp terraform.tfvars.example terraform.tfvars
$EDITOR backend.tfvars terraform.tfvars

# 4. Initial apply:
terraform init -backend-config=backend.tfvars
terraform plan -out=plan.tfplan
# Diff against ../../../../tests/terraform/wif/expected-plan.txt; material divergence = stop.
terraform apply plan.tfplan
```

## Post-bootstrap: lock down manual access

After the first apply, prod is "online." Capture outputs into the GitHub `prod` Environment's secrets, then **stop applying locally**:

```bash
gh api -X PUT repos/<owner>/<repo>/environments/prod
gh secret set WIF_PROVIDER         --env prod --body "$(terraform output -raw wif_provider_url)"
gh secret set WIF_SERVICE_ACCOUNT  --env prod --body "$(terraform output -raw github_deployer_email)"
```

From this point forward, future `terraform apply` against prod runs **only** through #10's `prod-deploy.yml` workflow under the GitHub Environments approval. Manual local applies are forbidden — the discipline is enforced by convention now and by branch-protection / required-reviewers once #10 lands.

## Tear-down

```bash
terraform destroy
```

**Caveat:** WIF pools and service accounts soft-delete for 30 days; the IDs (`github`, `github-deployer`) are reserved. Re-applying within 30 days requires undelete + import (see `docs/runbooks/terraform.md` "Common failure modes"). Don't destroy prod's auth substrate casually.
