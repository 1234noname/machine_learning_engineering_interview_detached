#!/usr/bin/env bash
# Idempotent bootstrap for a new GCP environment supporting AVSA's Terraform
# workflow. Wraps the seven hand-driven steps the human used to perform
# (and which the user discovered the hard way during #5's first apply):
# active project, billing link, ADC quota, state bucket, versioning + UBLA,
# bucket IAM, and a "next steps" pointer at terraform init.
#
# See docs/runbooks/terraform.md "Bootstrapping a new GCP environment".
#
# IMPORTANT: this script does NOT create the GCP project itself.
# Project IDs are precious (deletion burns the ID for all time); the operator
# creates the project manually, then runs this against it.

set -euo pipefail

DRY_RUN=0
ENV_NAME=""
PROJECT_ID=""
BILLING_ACCOUNT=""
BUCKET_NAME=""

print_help() {
    cat <<EOF
Usage: scripts/bootstrap-gcp-env.sh [--dry-run] <env> <project-id> <billing-account> [bucket-name]

Bootstrap a GCP project for AVSA Terraform. Idempotent — re-running is safe
and reports each step as already-done where applicable.

Args:
  env                 Environment name (dev / staging / prod).
  project-id          GCP project ID.
  billing-account     Billing account ID (e.g., 01234A-BCDEFG-HIJKLM).
  bucket-name         (Optional) State bucket name.
                      Default: <project-id>-terraform-state.

Flags:
  --dry-run           Narrate each step without modifying state. Skips all
                      gcloud / gsutil invocations; useful for testing and
                      for previewing what the script would do.
  -h, --help          Show this help.

Steps performed (each idempotent):
  1. Set gcloud active project to <project-id>.
  2. Link billing account (skip if already linked to the same account).
  3. Set ADC quota project.
  4. Create state bucket (skip if it already exists).
  5. Enable versioning + UBLA on the bucket (skip if already enabled).
  6. Grant calling identity roles/storage.admin on the bucket (skip if granted).
  7. Print next steps for terraform init.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) print_help; exit 0 ;;
        --) shift; break ;;
        --*)  echo "error: unknown flag: $1" >&2; exit 2 ;;
        *)
            if [[ -z "$ENV_NAME" ]]; then ENV_NAME="$1"
            elif [[ -z "$PROJECT_ID" ]]; then PROJECT_ID="$1"
            elif [[ -z "$BILLING_ACCOUNT" ]]; then BILLING_ACCOUNT="$1"
            elif [[ -z "$BUCKET_NAME" ]]; then BUCKET_NAME="$1"
            else echo "error: unexpected positional arg: $1" >&2; exit 2
            fi
            shift
            ;;
    esac
done

if [[ -z "$ENV_NAME" || -z "$PROJECT_ID" || -z "$BILLING_ACCOUNT" ]]; then
    echo "error: missing required args. Run with --help for usage." >&2
    exit 2
fi
BUCKET_NAME="${BUCKET_NAME:-${PROJECT_ID}-terraform-state}"

say() {
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "==> [dry-run] $*"
    else
        echo "==> $*"
    fi
}

# Wrap a command. Dry-run prints; live mode executes.
run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "    would run: $*"
        return 0
    fi
    "$@"
}

preflight() {
    if [[ $DRY_RUN -eq 1 ]]; then
        say "preflight: skipped (dry-run)"
        return
    fi
    if ! gcloud auth list --filter='status:ACTIVE' --format='value(account)' \
            | grep -q .; then
        echo "error: gcloud not authenticated. Run 'gcloud auth login'." >&2
        exit 1
    fi
    if ! gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1; then
        echo "error: project '$PROJECT_ID' not accessible. Create it first," >&2
        echo "       or check that you're authenticated as the right user." >&2
        exit 1
    fi
    say "preflight: gcloud authenticated; project '$PROJECT_ID' accessible"
}

step_set_project() {
    say "step 1: set active project to $PROJECT_ID"
    if [[ $DRY_RUN -eq 0 ]]; then
        local current
        current=$(gcloud config get-value project 2>/dev/null || echo "")
        if [[ "$current" == "$PROJECT_ID" ]]; then
            echo "    already set"
            return
        fi
    fi
    run gcloud config set project "$PROJECT_ID"
}

step_link_billing() {
    say "step 2: link billing account $BILLING_ACCOUNT"
    if [[ $DRY_RUN -eq 0 ]]; then
        local linked
        linked=$(gcloud billing projects describe "$PROJECT_ID" \
            --format='value(billingAccountName)' 2>/dev/null || echo "")
        if [[ "$linked" == "billingAccounts/$BILLING_ACCOUNT" ]]; then
            echo "    already linked"
            return
        fi
    fi
    run gcloud billing projects link "$PROJECT_ID" \
        --billing-account="$BILLING_ACCOUNT"
}

step_adc_quota() {
    say "step 3: set ADC quota project to $PROJECT_ID"
    # No reliable way to detect "is the quota project already set?" via the
    # CLI; the underlying command is idempotent so we always run it.
    run gcloud auth application-default set-quota-project "$PROJECT_ID"
}

step_create_bucket() {
    say "step 4: create state bucket gs://$BUCKET_NAME"
    if [[ $DRY_RUN -eq 0 ]]; then
        if gsutil ls -b "gs://$BUCKET_NAME" >/dev/null 2>&1; then
            echo "    bucket already exists"
            return
        fi
    fi
    run gsutil mb -l us-central1 -p "$PROJECT_ID" "gs://$BUCKET_NAME"
}

step_bucket_settings() {
    say "step 5: enable versioning + UBLA on gs://$BUCKET_NAME"
    if [[ $DRY_RUN -eq 0 ]]; then
        if gsutil versioning get "gs://$BUCKET_NAME" 2>/dev/null \
                | grep -q "Enabled"; then
            echo "    versioning: already enabled"
        else
            run gsutil versioning set on "gs://$BUCKET_NAME"
        fi
        if gsutil ubla get "gs://$BUCKET_NAME" 2>/dev/null \
                | grep -q "Enabled: True"; then
            echo "    UBLA: already enabled"
        else
            run gsutil ubla set on "gs://$BUCKET_NAME"
        fi
    else
        run gsutil versioning set on "gs://$BUCKET_NAME"
        run gsutil ubla set on "gs://$BUCKET_NAME"
    fi
}

step_bucket_iam() {
    say "step 6: grant caller storage.admin on gs://$BUCKET_NAME"
    local me
    if [[ $DRY_RUN -eq 0 ]]; then
        me=$(gcloud config get-value account)
    else
        me="<current-account>"
    fi
    # add-iam-policy-binding is idempotent (no-op if the binding exists).
    run gcloud storage buckets add-iam-policy-binding "gs://$BUCKET_NAME" \
        --member="user:$me" --role="roles/storage.admin"
}

step_next_steps() {
    say "step 7: bootstrap complete"
    cat <<EOF

Bootstrap complete for env='$ENV_NAME', project='$PROJECT_ID'.

Next steps:

  cd infra/terraform/environments/$ENV_NAME/shared
  cp backend.tfvars.example backend.tfvars
  cp terraform.tfvars.example terraform.tfvars
  \$EDITOR backend.tfvars terraform.tfvars
  # Set: bucket="$BUCKET_NAME"
  #      prefix="$ENV_NAME/shared"
  #      project_id="$PROJECT_ID"

  terraform init -backend-config=backend.tfvars
  terraform plan -out=plan.tfplan
  terraform apply plan.tfplan
EOF
}

say "bootstrapping env=$ENV_NAME project=$PROJECT_ID bucket=$BUCKET_NAME"
[[ $DRY_RUN -eq 1 ]] && echo "    DRY-RUN: no state will change"
preflight
step_set_project
step_link_billing
step_adc_quota
step_create_bucket
step_bucket_settings
step_bucket_iam
step_next_steps
