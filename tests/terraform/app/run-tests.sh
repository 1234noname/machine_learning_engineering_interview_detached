#!/usr/bin/env bash
# run-tests.sh — Test runner for modules/app/ and environments/*/app/.
#
# Usage:
#   bash tests/terraform/app/run-tests.sh
#   # or from repo root:
#   ./tests/terraform/app/run-tests.sh
#
# Preconditions (all must be true before running):
#   1. modules/app/ exists and is fully implemented.
#   2. modules/cluster/ exists and is fully implemented.
#   3. terraform, tflint, tfsec, and checkov are installed and on PATH.
#   4. No real GCP credentials are required for validate/lint steps.
#
# This script exits 0 only if ALL checks pass; exits 1 on the first failure
# with a clear diagnostic message.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
APP_MODULE="${REPO_ROOT}/infra/terraform/modules/app"
CLUSTER_MODULE="${REPO_ROOT}/infra/terraform/modules/cluster"
TERRAFORM_ROOT="${REPO_ROOT}/infra/terraform"
HELM_DIR="${REPO_ROOT}/helm"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}[PASS]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }
info() { echo -e "${YELLOW}[INFO]${NC} $*"; }

if [[ ! -d "${APP_MODULE}" ]]; then
  fail "modules/app/ does not exist at ${APP_MODULE}."
fi
if [[ ! -d "${CLUSTER_MODULE}" ]]; then
  fail "modules/cluster/ does not exist at ${CLUSTER_MODULE}."
fi

# ---------------------------------------------------------------------------
# A-series: Structural content checks (grep-based)
# ---------------------------------------------------------------------------

info "A1: helm/values.gke.yaml exists"
[[ -f "${HELM_DIR}/values.gke.yaml" ]] \
  || fail "A1 FAILED: helm/values.gke.yaml does not exist."
pass "A1: helm/values.gke.yaml exists."

info "A2: helm/values.gke.yaml contains nvidia.com/gpu"
grep -q 'nvidia\.com/gpu' "${HELM_DIR}/values.gke.yaml" \
  || fail "A2 FAILED: helm/values.gke.yaml does not contain 'nvidia.com/gpu'."
pass "A2: helm/values.gke.yaml contains nvidia.com/gpu."

info "A3: helm/values.gke.yaml contains avsa-ingress"
grep -q 'avsa-ingress' "${HELM_DIR}/values.gke.yaml" \
  || fail "A3 FAILED: helm/values.gke.yaml does not contain 'avsa-ingress'."
pass "A3: helm/values.gke.yaml contains avsa-ingress."

info "A4: skaffold.gke.yaml exists"
[[ -f "${REPO_ROOT}/skaffold.gke.yaml" ]] \
  || fail "A4 FAILED: skaffold.gke.yaml does not exist at repo root."
pass "A4: skaffold.gke.yaml exists."

info "A5: skaffold.gke.yaml contains 'gke' profile name"
grep -q 'name: gke' "${REPO_ROOT}/skaffold.gke.yaml" \
  || fail "A5 FAILED: skaffold.gke.yaml does not contain 'name: gke'."
pass "A5: skaffold.gke.yaml contains 'name: gke' profile."

info "A6: skaffold.gke.yaml contains googleCloudBuild"
grep -q 'googleCloudBuild' "${REPO_ROOT}/skaffold.gke.yaml" \
  || fail "A6 FAILED: skaffold.gke.yaml does not contain 'googleCloudBuild'."
pass "A6: skaffold.gke.yaml contains googleCloudBuild."

info "A7: modules/app/main.tf contains helm_release resource"
grep -q 'helm_release' "${APP_MODULE}/main.tf" \
  || fail "A7 FAILED: modules/app/main.tf does not contain 'helm_release'."
pass "A7: modules/app/main.tf contains helm_release."

info "A8: modules/app/variables.tf contains artifact_registry_host"
grep -q 'artifact_registry_host' "${APP_MODULE}/variables.tf" \
  || fail "A8 FAILED: modules/app/variables.tf does not declare 'artifact_registry_host'."
pass "A8: modules/app/variables.tf contains artifact_registry_host."

info "A9: modules/app/variables.tf contains cluster_endpoint"
grep -q 'cluster_endpoint' "${APP_MODULE}/variables.tf" \
  || fail "A9 FAILED: modules/app/variables.tf does not declare 'cluster_endpoint'."
pass "A9: modules/app/variables.tf contains cluster_endpoint."

info "A10: modules/app/variables.tf contains cluster_ca_certificate"
grep -q 'cluster_ca_certificate' "${APP_MODULE}/variables.tf" \
  || fail "A10 FAILED: modules/app/variables.tf does not declare 'cluster_ca_certificate'."
pass "A10: modules/app/variables.tf contains cluster_ca_certificate."

info "A11: modules/app/outputs.tf app_url is not null"
if grep -q 'value\s*=\s*null' "${APP_MODULE}/outputs.tf"; then
  fail "A11 FAILED: modules/app/outputs.tf still contains 'value = null'."
fi
pass "A11: modules/app/outputs.tf app_url is not null."

info "A12: modules/cluster/outputs.tf contains cluster_ca_certificate output"
grep -q 'cluster_ca_certificate' "${CLUSTER_MODULE}/outputs.tf" \
  || fail "A12 FAILED: modules/cluster/outputs.tf does not contain 'cluster_ca_certificate'."
pass "A12: modules/cluster/outputs.tf contains cluster_ca_certificate."

# ---------------------------------------------------------------------------
# B-series: terraform validate checks
# ---------------------------------------------------------------------------

info "B1: terraform validate — environments/staging/app/"
(
  cd "${REPO_ROOT}"
  terraform -chdir=infra/terraform/environments/staging/app \
    init -backend=false -upgrade -input=false > /dev/null 2>&1
  terraform -chdir=infra/terraform/environments/staging/app validate
) || fail "B1 FAILED: terraform validate failed for environments/staging/app/."
pass "B1: environments/staging/app/ validates cleanly."

info "B2: terraform validate — environments/prod/app/"
(
  cd "${REPO_ROOT}"
  terraform -chdir=infra/terraform/environments/prod/app \
    init -backend=false -upgrade -input=false > /dev/null 2>&1
  terraform -chdir=infra/terraform/environments/prod/app validate
) || fail "B2 FAILED: terraform validate failed for environments/prod/app/."
pass "B2: environments/prod/app/ validates cleanly."

info "B3: terraform validate — modules/app/"
(
  cd "${REPO_ROOT}"
  terraform -chdir=infra/terraform/modules/app \
    init -backend=false -upgrade -input=false > /dev/null 2>&1
  terraform -chdir=infra/terraform/modules/app validate
) || fail "B3 FAILED: terraform validate failed for modules/app/."
pass "B3: modules/app/ validates cleanly."

# ---------------------------------------------------------------------------
# C-series: Linting and security checks
# ---------------------------------------------------------------------------

info "C1: tflint --recursive from infra/terraform/"
(
  cd "${REPO_ROOT}"
  tflint --recursive --chdir="${TERRAFORM_ROOT}"
) || fail "C1 FAILED: tflint found issues."
pass "C1: tflint --recursive clean."

info "C2: tfsec infra/terraform/modules/app/ --no-color"
(
  cd "${REPO_ROOT}"
  tfsec infra/terraform/modules/app --no-color
) || fail "C2 FAILED: tfsec found security issues in modules/app/."
pass "C2: tfsec clean on modules/app/."

info "C3: checkov -d infra/terraform/modules/app/ --quiet --compact"
(
  cd "${REPO_ROOT}"
  checkov -d infra/terraform/modules/app --quiet --compact
) || fail "C3 FAILED: checkov found policy violations in modules/app/."
pass "C3: checkov clean on modules/app/."

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  All app tests passed.                 ${NC}"
echo -e "${GREEN}========================================${NC}"
exit 0
