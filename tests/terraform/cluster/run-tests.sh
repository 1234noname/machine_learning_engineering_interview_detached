#!/usr/bin/env bash
# run-tests.sh — Test runner for modules/cluster/ and environments/*/shared/.
#
# Usage:
#   bash tests/terraform/cluster/run-tests.sh
#   # or from repo root:
#   ./tests/terraform/cluster/run-tests.sh
#
# Preconditions (all must be true before running):
#   1. modules/cluster/ exists and is fully implemented.
#   2. environments/{dev,staging,prod}/shared/main.tf call module "cluster".
#   3. terraform, tflint, tfsec, and checkov are installed and on PATH.
#   4. No real GCP credentials are required for validate/lint steps.
#
# This script exits 0 only if ALL checks pass; exits 1 on the first failure
# with a clear diagnostic message.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
CLUSTER_MODULE="${REPO_ROOT}/infra/terraform/modules/cluster"
TERRAFORM_ROOT="${REPO_ROOT}/infra/terraform"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}[PASS]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }
info() { echo -e "${YELLOW}[INFO]${NC} $*"; }

if [[ ! -d "${CLUSTER_MODULE}" ]]; then
  fail "modules/cluster/ does not exist at ${CLUSTER_MODULE}."
fi

info "Step 1/5: terraform validate — environments/dev/shared"
(
  cd "${REPO_ROOT}"
  terraform -chdir=infra/terraform/environments/dev/shared init -backend=false -upgrade -input=false \
    > /dev/null 2>&1
  terraform -chdir=infra/terraform/environments/dev/shared validate
) || fail "terraform validate failed for environments/dev/shared."
pass "environments/dev/shared validates cleanly."

info "Step 2/5: terraform validate — environments/staging/shared"
(
  cd "${REPO_ROOT}"
  terraform -chdir=infra/terraform/environments/staging/shared init -backend=false -upgrade -input=false \
    > /dev/null 2>&1
  terraform -chdir=infra/terraform/environments/staging/shared validate
) || fail "terraform validate failed for environments/staging/shared."
pass "environments/staging/shared validates cleanly."

info "Step 3/5: terraform validate — environments/prod/shared"
(
  cd "${REPO_ROOT}"
  terraform -chdir=infra/terraform/environments/prod/shared init -backend=false -upgrade -input=false \
    > /dev/null 2>&1
  terraform -chdir=infra/terraform/environments/prod/shared validate
) || fail "terraform validate failed for environments/prod/shared."
pass "environments/prod/shared validates cleanly."

info "Step 4/5: tflint --recursive from infra/terraform/"
(
  cd "${REPO_ROOT}"
  tflint --recursive --chdir="${TERRAFORM_ROOT}"
) || fail "tflint found issues. Fix all tflint warnings before merging."
pass "tflint --recursive clean."

info "Step 5a/5: tfsec infra/terraform/modules/cluster/ --no-color"
(
  cd "${REPO_ROOT}"
  tfsec infra/terraform/modules/cluster --no-color
) || fail "tfsec found security issues in modules/cluster/. Fix or add a justified tfsec:ignore comment."
pass "tfsec clean on modules/cluster/."

info "Step 5b/5: checkov -d infra/terraform/modules/cluster/ --quiet --compact"
(
  cd "${REPO_ROOT}"
  checkov -d infra/terraform/modules/cluster --quiet --compact
) || fail "checkov found policy violations in modules/cluster/. Fix or suppress with a justified # checkov:skip comment."
pass "checkov clean on modules/cluster/."

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  All cluster tests passed.             ${NC}"
echo -e "${GREEN}========================================${NC}"
exit 0
