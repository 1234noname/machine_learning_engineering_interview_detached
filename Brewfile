# AVSA — Homebrew dependency manifest.
# Run `just setup` (or `brew bundle`) from repo root to install everything.
# This file is the dev-loop convenience layer. CI installs tools via dedicated
# setup actions; CI does NOT consume this Brewfile.

# Build / language tooling
brew "just"             # task runner
brew "uv"               # Python package + interpreter manager
brew "pre-commit"       # git-hook orchestrator

# Lint / format / security scanners
brew "lychee"           # markdown link checker (offline mode used)
brew "gitleaks"         # secret scanner
brew "yamlfmt"          # YAML formatter
brew "actionlint"       # GitHub Actions workflow linter
brew "hadolint"         # Dockerfile linter
brew "checkov"          # IaC static analysis (Terraform, etc.)

# Infrastructure tooling
brew "terraform"        # IaC
brew "tflint"           # Terraform linter
brew "tfsec"            # Terraform security scanner
brew "helm"             # Kubernetes package manager (used by `helm lint`)
brew "kubernetes-cli"   # kubectl — required for post-apply GPU DaemonSet and cluster access
cask "gcloud-cli"       # gcloud CLI (deploy target — see ADR 0003); was google-cloud-sdk

# Local Kubernetes dev loop (Track B — skaffold dev + minikube)
brew "minikube"         # local Kubernetes cluster
brew "skaffold"         # build + deploy loop for local k8s dev

# Container runtime (local; not used in CI)
brew "colima"           # Docker-compatible container runtime for macOS (replaces Docker Desktop)
brew "docker"           # Docker CLI; colima provides the daemon

# Database tooling
brew "libpq", link: true  # psql client for `just db-migrate` / `db-reset` (keg-only; force-linked onto PATH)

# Observability tooling
brew "prometheus"       # includes promtool (Prometheus config/rules validator)

# Elixir/OTP runtime (orchestrator service — ADR 0001)
brew "erlang"           # OTP 26; required by Elixir
brew "elixir"           # 1.16; pinned via .tool-versions
brew "buf"              # proto linting + codegen (buf generate)

# Node.js runtime (shopper frontend — ADR 0001)
brew "node"             # LTS; pin via .nvmrc
# pnpm is NOT installed via brew — its version is pinned in
# frontend/package.json's `packageManager` field, and the `setup` recipe
# installs that exact version globally via `npm install -g pnpm@<pinned>`.
# This avoids a brew↔npm dual-owner conflict on the `/opt/homebrew/bin/pnpm`
# symlink, and guarantees the project's pin is honoured cross-machine.

# GitHub / scripting
brew "gh"               # GitHub CLI (used by .githooks/pre-push and CI smoke)
brew "jq"               # JSON processor
brew "git-lfs"          # Git LFS — catalog dataset + embedding artifact (#090); `just setup` pulls it
