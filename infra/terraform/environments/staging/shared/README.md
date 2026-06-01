# `environments/staging/shared/` — AVSA staging shared infrastructure

The auth substrate every staging workflow rides on. Calls `../../../modules/wif/` to create the WIF pool, provider, deployer SA, and IAM binding. Apply this once; leave it up. CI-driven staging deploys authenticate as the deployer SA from this stack.

State lives at `gs://avsa-staging-terraform-state/staging/shared/`. Sibling: `../app/` — the per-deploy app stack (Track B+).

## One-time setup

See `docs/runbooks/release-pipeline.md` § "Staging one-time setup" for the full operator recipe, including GCP project creation, state bucket bootstrap, Terraform apply, and GitHub Environment + secret configuration.

## Tear-down

```bash
terraform destroy
```

**Caveat:** WIF pools and service accounts soft-delete for 30 days; the IDs (`github`, `github-deployer`) are reserved during that window. Re-applying within 30 days requires undelete + import. Don't destroy casually.
