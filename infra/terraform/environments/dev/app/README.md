# `environments/dev/app/` — AVSA dev app (ephemeral / per-PR) stack

Stub. Populated by Track B's first issue when actual application Terraform lands (GKE deployments, services, Cloud SQL, etc.).

## Intended shape

```
environments/dev/app/
├── main.tf              # backend prefix="dev/app/${name_suffix}"; calls modules/app
├── variables.tf         # includes var.name_suffix (set by the workflow per PR)
├── outputs.tf
├── backend.tfvars       # bucket only — prefix is computed from name_suffix
└── README.md
```

## Lifecycle

- **PR-ephemeral (dev only):** the workflow from #8 sets `var.name_suffix=pr-${{ github.event.number }}` and `prefix=dev/app/pr-${{ github.event.number }}` at `terraform init` time. On PR close, the workflow runs `terraform destroy` against the same prefix to tear down.
- **Persistent (staging, prod):** the equivalent `environments/{staging,prod}/app/` is a singleton — `name_suffix=staging` / `prod` and one state per env. Created by #9 / #10.

## Why split from `../shared/`

`shared/` holds the auth substrate (WIF). It must persist or no workflow can authenticate. This dir's resources (the actual app) come and go with PRs without touching the auth surface — separate state files = independent destroy semantics.

## When this lands

Track B's first issue. At that point this README is replaced by the real config.
