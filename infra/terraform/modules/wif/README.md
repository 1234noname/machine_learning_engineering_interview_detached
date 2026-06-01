# `modules/wif/` — Workload Identity Federation module

Reusable Terraform module wrapping AVSA's GitHub-Actions-to-GCP authentication. Used by every environment under `../environments/`.

## What it creates

- API enables: `iam.googleapis.com`, `iamcredentials.googleapis.com`, `cloudresourcemanager.googleapis.com` (`disable_on_destroy = false`).
- `google_iam_workload_identity_pool.github`.
- `google_iam_workload_identity_pool_provider.github` with `attribute_condition` gating tokens to the configured GitHub org.
- `google_service_account.github_deployer`.
- `google_service_account_iam_binding.github_deployer_wif` with `principalSet` scoped to the configured repository.
- `google_project_iam_member.deployer_token_creator` granting only `roles/iam.serviceAccountTokenCreator` at project level.

## Inputs

| Variable | Type | Description |
|---|---|---|
| `project_id` | string | GCP project ID where WIF resources live |
| `github_repository` | string | `OWNER/REPO` — principalSet is scoped to this exact repo |
| `github_repository_owner` | string | GitHub org/user — used in `attribute_condition` |

## Outputs

| Output | Description |
|---|---|
| `wif_provider_url` | Full resource URL of the WIF provider; the env re-exports this as the `WIF_PROVIDER` GitHub Actions secret |
| `github_deployer_email` | Deployer SA email; re-exported as the `WIF_SERVICE_ACCOUNT` secret |

## Usage

```hcl
module "wif" {
  source                  = "../../modules/wif"
  project_id              = var.project_id
  github_repository       = var.github_repository
  github_repository_owner = var.github_repository_owner
}
```

The module declares only `required_providers` — provider configuration (`provider "google" { project = ... }`) is the environment's responsibility, so the same module works against any account.

## Security guards

Three load-bearing checks are enforced by this module:

1. **`attribute_condition`** on the WIF provider — only tokens from `var.github_repository_owner` are accepted (issuance gate).
2. **`principalSet`** on the SA IAM binding — only tokens from `var.github_repository` can impersonate the deployer (impersonation gate).
3. **Project-level deployer role limited** to `roles/iam.serviceAccountTokenCreator` — minimum required; broader roles are added at module-call sites in environments where actually needed.

Inline `checkov:skip` and `tfsec:ignore` comments document why broader checks are intentionally bypassed. Don't remove without consulting `docs/runbooks/terraform.md` and the original [](../../../../issues/completed/005-wif-terraform.md).
