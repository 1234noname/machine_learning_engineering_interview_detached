#!/usr/bin/env bash
# deploy-prod.sh — AVSA end-to-end manual production deploy script.
#
# Runs five sequential phases from the local machine using Application Default
# Credentials (ADC). No static service-account keys are used or required.
#
# Prerequisites:
#   gcloud auth application-default login
#   terraform  (>= 1.5.0) on PATH
#   helm       (>= 3.x)   on PATH
#   kubectl               on PATH
#   docker                on PATH
#   gh (GitHub CLI)       on PATH — for post-deploy secret capture
#
# Usage:
#   GCP_PROJECT_ID=avsa-prd bash scripts/deploy-prod.sh
#
# Optional overrides:
#   GCP_REGION        (default: us-central1)
#   TF_STATE_BUCKET   (default: ${GCP_PROJECT_ID}-terraform-state)
#   HELM_RELEASE      (default: avsa)
#   HELM_NAMESPACE    (default: avsa)
#   GIT_SHA               (default: short HEAD SHA — used as image tag before digest pinning)
#   AVSA_MODAL_MODEL_URL  (default: empty — used as AVSA_MODEL_URL in the cluster secret;
#                          set to the deployed Modal /embed endpoint URL if any GKE service
#                          needs to reach the model service directly)
#
# The script is intentionally verbose: every step prints what it is doing so
# the operator can follow along.
#
# See docs/runbooks/release-pipeline.md "Manual first-deploy procedure"
# for the full operator checklist, including post-deploy secret-capture steps.

set -euo pipefail

# ---------------------------------------------------------------------------
# Phase 0 — Guards and config
# ---------------------------------------------------------------------------

PROJECT_ID="${GCP_PROJECT_ID:-}"
REGION="${GCP_REGION:-us-central1}"
STATE_BUCKET="${TF_STATE_BUCKET:-${PROJECT_ID}-terraform-state}"
HELM_RELEASE="${HELM_RELEASE:-avsa}"
HELM_NAMESPACE="${HELM_NAMESPACE:-avsa}"
GIT_SHA="${GIT_SHA:-$(git rev-parse --short HEAD)}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: GCP_PROJECT_ID must be set." >&2
  echo "  Example: GCP_PROJECT_ID=avsa-prd bash scripts/deploy-prod.sh" >&2
  exit 1
fi

# Verify ADC is configured — gcloud exits non-zero if not logged in.
if ! gcloud auth application-default print-access-token > /dev/null 2>&1; then
  echo "ERROR: Application Default Credentials not configured." >&2
  echo "  Run: gcloud auth application-default login" >&2
  exit 1
fi

# Terraform's GCS backend does not reliably pick up ADC on all platforms.
# Exporting an explicit access token makes the GCS backend auth deterministic.
export GOOGLE_OAUTH_ACCESS_TOKEN
GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth application-default print-access-token)"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_TF_DIR="${REPO_ROOT}/infra/terraform/environments/prod/shared"

echo ""
echo "======================================================================"
echo "AVSA prod — end-to-end manual deploy"
echo "Project  : ${PROJECT_ID}"
echo "Region   : ${REGION}"
echo "State    : gs://${STATE_BUCKET}"
echo "Git SHA  : ${GIT_SHA}"
echo "Repo     : ${REPO_ROOT}"
echo "======================================================================"
echo ""

# ---------------------------------------------------------------------------
# Phase 1 — Terraform apply: prod/shared (cluster + WIF infrastructure)
# ---------------------------------------------------------------------------
echo "[Phase 1/5] terraform apply — environments/prod/shared"
echo "            Provisions: GKE cluster, GPU node pool, Cloud SQL,"
echo "            Artifact Registry, Secret Manager entries, WIF pool + SA."
echo ""

# Ensure the GCS state bucket exists (idempotent).
if gcloud storage buckets describe "gs://${STATE_BUCKET}" > /dev/null 2>&1; then
  echo "  State bucket already exists: ${STATE_BUCKET}"
else
  echo "  Creating GCS state bucket: ${STATE_BUCKET}"
  gcloud storage buckets create "gs://${STATE_BUCKET}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --uniform-bucket-level-access
  gcloud storage buckets update "gs://${STATE_BUCKET}" \
    --project="${PROJECT_ID}" \
    --versioning
  # Wait for GCS bucket creation to propagate before Terraform backend init.
  sleep 10
fi

(
  cd "${SHARED_TF_DIR}"
  terraform init \
    -backend-config="bucket=${STATE_BUCKET}" \
    -backend-config="prefix=prod/shared" \
    -reconfigure \
    -input=false

  terraform apply \
    -var="project_id=${PROJECT_ID}" \
    -var="region=${REGION}" \
    -auto-approve \
    -input=false
)

echo "[Phase 1/5] DONE — shared infrastructure applied."
echo ""

# Capture Terraform outputs for use in subsequent phases.
CLUSTER_NAME=$(cd "${SHARED_TF_DIR}" && terraform output -raw cluster_name 2>/dev/null || true)
ARTIFACT_REGISTRY_HOST=$(cd "${SHARED_TF_DIR}" && terraform output -raw artifact_registry_host 2>/dev/null || true)
DB_CONNECTION_NAME=$(cd "${SHARED_TF_DIR}" && terraform output -raw db_connection_name 2>/dev/null || true)
WIF_PROVIDER_URL=$(cd "${SHARED_TF_DIR}" && terraform output -raw wif_provider_url 2>/dev/null || true)
DEPLOYER_SA_EMAIL=$(cd "${SHARED_TF_DIR}" && terraform output -raw github_deployer_email 2>/dev/null || true)

echo "  cluster_name          : ${CLUSTER_NAME}"
echo "  artifact_registry_host: ${ARTIFACT_REGISTRY_HOST}"
echo "  db_connection_name    : ${DB_CONNECTION_NAME}"
echo "  wif_provider_url      : ${WIF_PROVIDER_URL}"
echo "  deployer_sa_email     : ${DEPLOYER_SA_EMAIL}"
echo ""

# Configure kubectl for the provisioned cluster.
gcloud container clusters get-credentials "${CLUSTER_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}"

IMAGE_REPO="${ARTIFACT_REGISTRY_HOST}/${PROJECT_ID}/avsa"

# ---------------------------------------------------------------------------
# Phase 1.5 — Secrets bootstrap and Cloud SQL database setup
# ---------------------------------------------------------------------------
# Terraform creates the Cloud SQL instance and Secret Manager secret *names*
# but no values. This phase:
#   a) reads sensitive keys from .env (operator-managed, never committed)
#   b) generates the DB password on first run; reads it from Secret Manager
#      on subsequent runs
#   c) creates the Cloud SQL database + user (idempotent)
#   d) opens an authorized network (0.0.0.0/0, SSL enforced) — stopgap until
# brings private IP + Cloud SQL Auth Proxy sidecars
#   e) creates / refreshes the avsa-env Kubernetes secret consumed by all pods
# ---------------------------------------------------------------------------
echo "[Phase 1.5/5] Secrets bootstrap and Cloud SQL setup"
echo ""

# Source .env for AVSA_ANTHROPIC_API_KEY, AVSA_MCP_API_KEY, and other secrets.
# When running from a git worktree the .env lives in the main worktree root;
# fall back there if not found in REPO_ROOT.
_env_file="${REPO_ROOT}/.env"
if [[ ! -f "${_env_file}" ]]; then
  _main_wt=$(git -C "${REPO_ROOT}" worktree list --porcelain | awk 'NR==1{print $2}')
  _env_file="${_main_wt}/.env"
fi
if [[ -f "${_env_file}" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${_env_file}"
  set +a
fi

if [[ -z "${AVSA_ANTHROPIC_API_KEY:-}" ]]; then
  echo "ERROR: AVSA_ANTHROPIC_API_KEY is not set." >&2
  echo "  Add it to ${REPO_ROOT}/.env or export it before running this script." >&2
  exit 1
fi

# DB password: read from Secret Manager if a version already exists;
# generate and store on first run.
DB_SECRET="AVSA_DB_PASSWORD"
EXISTING_VER=$(gcloud secrets versions list "${DB_SECRET}" \
  --project="${PROJECT_ID}" --filter="state=ENABLED" \
  --format='value(name)' 2>/dev/null | head -1 || true)

if [[ -n "${EXISTING_VER}" ]]; then
  echo "  DB password: reading existing version from Secret Manager."
  DB_PASSWORD=$(gcloud secrets versions access latest \
    --secret="${DB_SECRET}" --project="${PROJECT_ID}")
else
  echo "  DB password: generating and storing in Secret Manager (first run)."
  DB_PASSWORD=$(openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c 32)
  printf '%s' "${DB_PASSWORD}" | gcloud secrets versions add "${DB_SECRET}" \
    --data-file=- --project="${PROJECT_ID}"
fi

# Cloud SQL public IP.
CLOUD_SQL_IP=$(gcloud sql instances describe "avsa-prod" \
  --project="${PROJECT_ID}" \
  --format='value(ipAddresses[0].ipAddress)')
echo "  Cloud SQL IP : ${CLOUD_SQL_IP}"

# Create database (idempotent).
gcloud sql databases create avsa \
  --instance="avsa-prod" --project="${PROJECT_ID}" 2>/dev/null \
  || echo "  Database 'avsa' already exists — skipping create."

# Create user (may already exist) then always sync the password.
gcloud sql users create avsa \
  --instance="avsa-prod" --password="${DB_PASSWORD}" \
  --project="${PROJECT_ID}" 2>/dev/null \
  || echo "  User 'avsa' already exists — will sync password."
gcloud sql users set-password avsa \
  --instance="avsa-prod" --password="${DB_PASSWORD}" \
  --project="${PROJECT_ID}" --quiet

# Open Cloud SQL to all source IPs; SSL (ENCRYPTED_ONLY) is enforced by
# Terraform. TODO: remove when private IP + Auth Proxy sidecars land.
echo "  Authorizing 0.0.0.0/0 on Cloud SQL (SSL enforced; tighten in)."
gcloud sql instances patch "avsa-prod" \
  --project="${PROJECT_ID}" \
  --authorized-networks="0.0.0.0/0" \
  --quiet

DATABASE_URL="postgresql://avsa:${DB_PASSWORD}@${CLOUD_SQL_IP}/avsa?sslmode=require"

# Grafana admin password: same bootstrap pattern as DB_SECRET.
GRAFANA_SECRET="AVSA_GRAFANA_ADMIN_PASSWORD"
EXISTING_GRAFANA_VER=$(gcloud secrets versions list "${GRAFANA_SECRET}" \
  --project="${PROJECT_ID}" --filter="state=ENABLED" \
  --format='value(name)' 2>/dev/null | head -1 || true)
if [[ -n "${EXISTING_GRAFANA_VER}" ]]; then
  echo "  Grafana password: reading existing version from Secret Manager."
  GRAFANA_ADMIN_PASSWORD=$(gcloud secrets versions access latest \
    --secret="${GRAFANA_SECRET}" --project="${PROJECT_ID}")
else
  echo "  Grafana password: generating and storing in Secret Manager (first run)."
  GRAFANA_ADMIN_PASSWORD=$(openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c 32)
  printf '%s' "${GRAFANA_ADMIN_PASSWORD}" | gcloud secrets versions add "${GRAFANA_SECRET}" \
    --data-file=- --project="${PROJECT_ID}"
fi

# Create the namespace early (helm --create-namespace may race with kubectl below).
kubectl create namespace "${HELM_NAMESPACE}" --dry-run=client -o yaml \
  | kubectl apply -f -

# Create or refresh avsa-env — all pods consume this via envFrom.
echo "  Applying avsa-env secret to namespace ${HELM_NAMESPACE} ..."
# Generate a stable SECRET_KEY_BASE for the Elixir orchestrator: read from
# Secret Manager if present; generate and store on first run (same pattern as
# the DB password above so the key survives re-deploys without a new release).
_SKB_SECRET="AVSA_SECRET_KEY_BASE"
_EXISTING_SKB=$(gcloud secrets versions list "${_SKB_SECRET}" \
  --project="${PROJECT_ID}" --filter="state=ENABLED" \
  --format='value(name)' 2>/dev/null | head -1 || true)
if [[ -n "${_EXISTING_SKB}" ]]; then
  SECRET_KEY_BASE=$(gcloud secrets versions access latest \
    --secret="${_SKB_SECRET}" --project="${PROJECT_ID}")
else
  echo "  SECRET_KEY_BASE: generating and storing in Secret Manager (first run)."
  SECRET_KEY_BASE=$(openssl rand -hex 64)
  printf '%s' "${SECRET_KEY_BASE}" | gcloud secrets versions add "${_SKB_SECRET}" \
    --data-file=- --project="${PROJECT_ID}" 2>/dev/null \
    || { gcloud secrets create "${_SKB_SECRET}" --project="${PROJECT_ID}" \
           --replication-policy=automatic --quiet 2>/dev/null || true
         printf '%s' "${SECRET_KEY_BASE}" | gcloud secrets versions add "${_SKB_SECRET}" \
           --data-file=- --project="${PROJECT_ID}"; }
fi

kubectl create secret generic avsa-env \
  --namespace "${HELM_NAMESPACE}" \
  --from-literal="DATABASE_URL=${DATABASE_URL}" \
  --from-literal="AVSA_DB_URL=${DATABASE_URL}" \
  --from-literal="AVSA_ANTHROPIC_API_KEY=${AVSA_ANTHROPIC_API_KEY}" \
  --from-literal="AVSA_MCP_API_KEY=${AVSA_MCP_API_KEY:?AVSA_MCP_API_KEY must be set in .env}" \
  --from-literal="AVSA_STORAGE_HMAC_SECRET=${AVSA_STORAGE_HMAC_SECRET:-}" \
  --from-literal="AVSA_API_URL=http://api-service" \
  --from-literal="AVSA_PROFILE=prod" \
  --from-literal="AVSA_BATCHER_URL=http://batcher-service" \
  --from-literal="AVSA_MODEL_URL=${AVSA_MODAL_MODEL_URL:-}" \
  --from-literal="AVSA_MODEL_STUB=1" \
  --from-literal="BATCHER_PORT=8001" \
  --from-literal="AVSA_ORCHESTRATOR_ADDR=orchestrator-service:50051" \
  --from-literal="SECRET_KEY_BASE=${SECRET_KEY_BASE}" \
  --from-literal="PHX_HOST=orchestrator-service" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "[Phase 1.5/5] DONE — secrets bootstrapped, Cloud SQL database ready."
echo ""

# ---------------------------------------------------------------------------
# Phase 1.9 — Generate prod config (must exist before Cloud Build tarball)
# ---------------------------------------------------------------------------
# config/avsa.toml is gitignored (it's env-specific) but baked into the Docker
# images at build time. Generate it here so gcloud builds submit picks it up.
# .gcloudignore explicitly allows config/avsa.toml through while excluding data/.
echo "[Phase 1.9/5] Generating config/avsa.toml (prod profile)"
uv run python "${REPO_ROOT}/scripts/config-gen.py" prod
echo "[Phase 1.9/5] DONE — config/avsa.toml ready."
echo ""

# ---------------------------------------------------------------------------
# Phase 2 — Build and push all 5 Docker images via Cloud Build
# ---------------------------------------------------------------------------
# Cloud Build runs on GCP workers (native linux/amd64, 100 GB disk) and pushes
# directly to Artifact Registry — no local Docker disk or QEMU emulation.
echo "[Phase 2/5] Build and push Docker images via Cloud Build (4 GKE services)"
echo "            Registry : ${ARTIFACT_REGISTRY_HOST}"
echo "            Repo     : ${IMAGE_REPO}"
echo "            Tag      : ${GIT_SHA}"
echo ""

cd "${REPO_ROOT}"

# Ensure Cloud Build API is enabled (idempotent; no-op if already on).
gcloud services enable cloudbuild.googleapis.com --project="${PROJECT_ID}" --quiet

for SERVICE in api batcher orchestrator shopper; do
  IMAGE="${IMAGE_REPO}/${SERVICE}:${GIT_SHA}"
  echo "  [${SERVICE}] Submitting Cloud Build for ${IMAGE} ..."
  # gcloud builds submit --tag only supports a root-level Dockerfile; use
  # --config with an inline temp file to specify dockerfiles/Dockerfile.SERVICE.
  _cbcfg=$(mktemp /tmp/cloudbuild-XXXXXX)  # no .yaml suffix — BSD mktemp requires Xs at end
  # Shopper gets extra build args so next.config.js and navbar links are baked
  # with the correct prod URLs. Grafana and API docs IPs are fetched here if
  # already assigned (idempotent on re-runs); empty string hides the link.
  if [[ "${SERVICE}" == "shopper" ]]; then
    _grafana_ip=$(kubectl get svc kube-prometheus-stack-grafana \
      --namespace monitoring \
      --output jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
    _api_ingress_ip=$(kubectl get ingress \
      --namespace "${HELM_NAMESPACE}" \
      --output jsonpath='{.items[0].status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
    _grafana_url="${_grafana_ip:+http://${_grafana_ip}/d/avsa-baseline}"
    _api_docs_url="${_api_ingress_ip:+http://${_api_ingress_ip}/docs}"
    _extra_build_args="'--build-arg', 'AVSA_API_URL=http://api-service', '--build-arg', 'NEXT_PUBLIC_GRAFANA_URL=${_grafana_url}', '--build-arg', 'NEXT_PUBLIC_API_DOCS_URL=${_api_docs_url}',"
  else
    _extra_build_args=""
  fi
  cat > "${_cbcfg}" <<CBEOF
steps:
- name: 'gcr.io/cloud-builders/docker'
  env: ['DOCKER_BUILDKIT=1']
  args: ['build', '-f', 'dockerfiles/Dockerfile.${SERVICE}', '-t', '${IMAGE}', ${_extra_build_args} '.']
images: ['${IMAGE}']
CBEOF
  gcloud builds submit \
    --config "${_cbcfg}" \
    --project="${PROJECT_ID}" \
    --timeout=3600 \
    .
  rm -f "${_cbcfg}"
  echo "  [${SERVICE}] Build + push complete."
done

echo ""
echo "  All images pushed. Fetching digests from Artifact Registry ..."
echo ""

DIGEST_API=$(gcloud artifacts docker images describe \
  "${IMAGE_REPO}/api:${GIT_SHA}" --project="${PROJECT_ID}" \
  --format='value(image_summary.digest)')
DIGEST_BATCHER=$(gcloud artifacts docker images describe \
  "${IMAGE_REPO}/batcher:${GIT_SHA}" --project="${PROJECT_ID}" \
  --format='value(image_summary.digest)')
DIGEST_ORCHESTRATOR=$(gcloud artifacts docker images describe \
  "${IMAGE_REPO}/orchestrator:${GIT_SHA}" --project="${PROJECT_ID}" \
  --format='value(image_summary.digest)')
DIGEST_SHOPPER=$(gcloud artifacts docker images describe \
  "${IMAGE_REPO}/shopper:${GIT_SHA}" --project="${PROJECT_ID}" \
  --format='value(image_summary.digest)')

echo "  api          digest: ${DIGEST_API}"
echo "  batcher      digest: ${DIGEST_BATCHER}"
echo "  orchestrator digest: ${DIGEST_ORCHESTRATOR}"
echo "  shopper      digest: ${DIGEST_SHOPPER}"
echo ""
echo "[Phase 2/5] DONE — 4 GKE images built, pushed, and digests captured."
echo ""

# ---------------------------------------------------------------------------
# Phase 2.5 — Deploy kube-prometheus-stack (Prometheus + Grafana)
# ---------------------------------------------------------------------------
# Installed before the AVSA chart so the ServiceMonitor CRDs exist when
# Helm processes the AVSA ServiceMonitor templates.
echo "[Phase 2.5/5] Deploy kube-prometheus-stack into namespace monitoring"
echo ""

MONITORING_NAMESPACE="monitoring"
# Pin to an exact semver — check https://github.com/prometheus-community/helm-charts/releases
# for the latest stable release before redeploying.
KPS_VERSION="65.8.1"

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts \
  --force-update 2>/dev/null || true
helm repo update prometheus-community

kubectl create namespace "${MONITORING_NAMESPACE}" --dry-run=client -o yaml \
  | kubectl apply -f -

helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --version "${KPS_VERSION}" \
  --namespace "${MONITORING_NAMESPACE}" \
  --values "${REPO_ROOT}/infra/helm/monitoring-values.yaml" \
  --set "grafana.adminPassword=${GRAFANA_ADMIN_PASSWORD}" \
  --wait \
  --timeout=10m

# Wait for the ServiceMonitor CRD to be fully established before proceeding.
kubectl wait --for=condition=established \
  crd/servicemonitors.monitoring.coreos.com \
  --timeout=120s

echo ""
echo "[Phase 2.5/5] DONE — Prometheus + Grafana deployed."
echo ""

# ---------------------------------------------------------------------------
# Phase 2.7 — Deploy Modal model service (serverless A10G GPU endpoint)
# ---------------------------------------------------------------------------
# The model service runs on Modal, not GKE. Deploy it before the helm upgrade
# so the batcher ConfigMap (vitServiceUrl) points at a live endpoint.
#
# Prerequisites:
#   modal token new   (or MODAL_TOKEN_ID + MODAL_TOKEN_SECRET env vars)
#   modal is a dev dep in the root uv workspace — always invoked via `uv run modal`
#
# The deployed URL is deterministic:
#   https://<workspace>--avsa-model-model-api.modal.run/embed
# It is baked into helm/values.yaml (batcher.vitServiceUrl); no dynamic
# URL passing is needed unless the workspace name changes.
# ---------------------------------------------------------------------------
echo "[Phase 2.7/5] Deploy Modal model service (A10G GPU)"
echo ""

if ! uv run modal profile list &>/dev/null 2>&1; then
  echo "ERROR: Modal is not authenticated. Run: uv run modal token new" >&2
  exit 1
fi

echo "  Deploying avsa-model to Modal ..."
cd "${REPO_ROOT}"
uv run modal deploy modal_deploy/model_app.py

echo ""
echo "[Phase 2.7/5] DONE — Modal model service deployed."
echo ""

# ---------------------------------------------------------------------------
# Phase 3 — helm upgrade --install (prod overlay with digest-pinned tags)
# ---------------------------------------------------------------------------
echo "[Phase 3/5] helm upgrade --install — ${HELM_RELEASE} into namespace ${HELM_NAMESPACE}"
echo "            Using digest-pinned tags — never :latest."
echo ""

helm upgrade --install "${HELM_RELEASE}" "${REPO_ROOT}/helm" \
  --namespace "${HELM_NAMESPACE}" \
  --create-namespace \
  --values "${REPO_ROOT}/helm/values.yaml" \
  --values "${REPO_ROOT}/helm/values.gke.yaml" \
  --values "${REPO_ROOT}/helm/values.prod.yaml" \
  --set "image_api.repository=${IMAGE_REPO}/api" \
  --set "image_api.tag=${GIT_SHA}" \
  --set "image_api.digest=${DIGEST_API}" \
  --set "image_batcher.repository=${IMAGE_REPO}/batcher" \
  --set "image_batcher.tag=${GIT_SHA}" \
  --set "image_batcher.digest=${DIGEST_BATCHER}" \
  --set "image_orchestrator.repository=${IMAGE_REPO}/orchestrator" \
  --set "image_orchestrator.tag=${GIT_SHA}" \
  --set "image_orchestrator.digest=${DIGEST_ORCHESTRATOR}" \
  --set "image_shopper.repository=${IMAGE_REPO}/shopper" \
  --set "image_shopper.tag=${GIT_SHA}" \
  --set "image_shopper.digest=${DIGEST_SHOPPER}" \
  --set "env.AVSA_PROFILE=prod" \
  --wait \
  --timeout=15m

echo ""
echo "[Phase 3/5] DONE — Helm release deployed and all pods ready."
echo ""

# ---------------------------------------------------------------------------
# Phase 4 — Seed pgvector catalog (5 000 synthetic products)
# ---------------------------------------------------------------------------
echo "[Phase 4/5] Seed pgvector catalog"
echo "            This runs catalog_seed.copy_rows() inside the api pod."
echo "            Seeding 5 000 rows — this may take a few minutes."
echo ""

API_POD=$(kubectl get pod \
  --namespace "${HELM_NAMESPACE}" \
  --selector app=api-service \
  --output jsonpath='{.items[0].metadata.name}')

echo "  Target pod: ${API_POD}"

kubectl exec \
  --namespace "${HELM_NAMESPACE}" \
  "${API_POD}" \
  -- \
  uv run python -c "
import os
os.environ['AVSA_PROFILE'] = 'prod'
from machine_learning_engineering_interview import catalog_seed
import psycopg
dsn = os.environ['DATABASE_URL']
with psycopg.connect(dsn) as conn:
    written = catalog_seed.copy_rows(conn, (catalog_seed.synthetic_product(i) for i in range(5000)))
    conn.commit()
print(f'seeded {written} rows')
"

echo ""
echo "[Phase 4/5] DONE — catalog seeded."
echo ""

# ---------------------------------------------------------------------------
# Phase 5 — Smoke suite
# ---------------------------------------------------------------------------
echo "[Phase 5/5] Production smoke gate"
echo ""

# Prefer the external ingress IP; fall back to ClusterIP for in-cluster checks.
APP_URL=$(kubectl get ingress \
  --namespace "${HELM_NAMESPACE}" \
  --output jsonpath='{.items[0].status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)

if [[ -z "${APP_URL}" ]]; then
  echo "  Ingress IP not yet assigned; falling back to api-service ClusterIP."
  APP_URL="$(kubectl get svc api-service \
    --namespace "${HELM_NAMESPACE}" \
    --output jsonpath='{.spec.clusterIP}'):80"
fi

echo "  APP_URL: http://${APP_URL}"
echo ""

AVSA_PROD_API_URL="http://${APP_URL}" bash "${REPO_ROOT}/scripts/smoke-prod.sh"

echo ""
echo "[Phase 5/5] DONE — smoke suite passed."
echo ""

# Fetch public endpoints now that LoadBalancers have settled.
API_INGRESS_IP=$(kubectl get ingress \
  --namespace "${HELM_NAMESPACE}" \
  --output jsonpath='{.items[0].status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
SHOPPER_IP=$(kubectl get svc shopper-service \
  --namespace "${HELM_NAMESPACE}" \
  --output jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
GRAFANA_IP=$(kubectl get svc kube-prometheus-stack-grafana \
  --namespace monitoring \
  --output jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)

# ---------------------------------------------------------------------------
# Closing summary
# ---------------------------------------------------------------------------
echo "======================================================================"
echo "AVSA prod — deploy complete"
echo ""
echo "Public endpoints:"
echo "  API     : http://${API_INGRESS_IP:-<ingress-pending>}/"
echo "  Shopper : http://${SHOPPER_IP:-<lb-pending>}/"
echo "  Grafana : http://${GRAFANA_IP:-<lb-pending>}/ (admin / ${GRAFANA_ADMIN_PASSWORD})"
echo "  (If IPs show <pending>, run: kubectl get svc,ingress -n ${HELM_NAMESPACE} && kubectl get svc -n monitoring)"
echo ""
echo "Image digests (record these for the audit log):"
echo "  api          ${GIT_SHA}@${DIGEST_API}"
echo "  batcher      ${GIT_SHA}@${DIGEST_BATCHER}"
echo "  orchestrator ${GIT_SHA}@${DIGEST_ORCHESTRATOR}"
echo "  shopper      ${GIT_SHA}@${DIGEST_SHOPPER}"
echo "  model        (Modal serverless — see modal app show avsa-model for revision)"
echo ""
echo "WIF infrastructure outputs (capture into GitHub 'prod' environment secrets):"
echo "  WIF_PROVIDER_URL  : ${WIF_PROVIDER_URL}"
echo "  DEPLOYER_SA_EMAIL : ${DEPLOYER_SA_EMAIL}"
echo ""
echo "Run the following to register the GitHub secrets:"
echo "  gh api -X PUT repos/<owner>/avsa/environments/prod"
echo "  gh secret set WIF_PROVIDER        --env prod --body '${WIF_PROVIDER_URL}'"
echo "  gh secret set WIF_SERVICE_ACCOUNT --env prod --body '${DEPLOYER_SA_EMAIL}'"
echo ""
echo "After confirming smoke passes, enable the automated pipeline:"
echo "  Set DEPLOY_ENABLED=true in the GitHub repo variables."
echo "  (Settings → Variables → Repository variables → DEPLOY_ENABLED)"
echo "======================================================================"
