#!/usr/bin/env bash
#
# Provision the Market Indicators dashboard infrastructure on Azure App
# Service (Linux, Python). One-time / idempotent — re-running updates the
# existing resources. Ongoing code deploys are handled by GitHub Actions
# (.github/workflows/deploy.yml, OIDC) — not this script.
#
# Prerequisites:
#   - Azure CLI installed and logged in:  az login
#   - The target subscription selected:   az account set --subscription <id>
#   - FRED_API_KEY exported in your shell (it is set as an App Service
#     Application Setting; config.get_fred_api_key() reads it from the env).
#
# Usage:
#   FRED_API_KEY=xxxx ./scripts/azure-provision.sh
#   APP_NAME=my-unique-name LOCATION=switzerlandnorth ./scripts/azure-provision.sh
#
# After it finishes: give GitHub Actions an OIDC identity with Website
# Contributor on the app — see "CI/CD" in README.md's Deploy section.
#
set -euo pipefail

# --- Configuration (override any of these via environment variables) --------
RESOURCE_GROUP="${RESOURCE_GROUP:-market-dashboard-rg}"
LOCATION="${LOCATION:-switzerlandnorth}"   # westeurope rejects new subscriptions
PLAN_NAME="${PLAN_NAME:-market-dashboard-plan}"
SKU="${SKU:-B1}"                          # B1 supports Always On + websockets
APP_NAME="${APP_NAME:-key-indicators-dashboard}"   # must be globally unique
PYTHON_VERSION="${PYTHON_VERSION:-3.11}"

# --- Preflight --------------------------------------------------------------
command -v az >/dev/null || { echo "ERROR: Azure CLI (az) not found." >&2; exit 1; }
az account show >/dev/null 2>&1 || { echo "ERROR: run 'az login' first." >&2; exit 1; }
: "${FRED_API_KEY:?Set FRED_API_KEY in your environment before deploying}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> Resource group: $RESOURCE_GROUP ($LOCATION)"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

echo "==> App Service plan: $PLAN_NAME ($SKU, Linux)"
az appservice plan create \
  --name "$PLAN_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --sku "$SKU" \
  --is-linux \
  --output none

echo "==> Web app: $APP_NAME (Python $PYTHON_VERSION)"
az webapp create \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --plan "$PLAN_NAME" \
  --runtime "PYTHON:$PYTHON_VERSION" \
  --output none

echo "==> Application settings (FRED key + Oryx build)"
az webapp config appsettings set \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --settings \
    "FRED_API_KEY=$FRED_API_KEY" \
    SCM_DO_BUILD_DURING_DEPLOYMENT=true \
    WEBSITES_PORT=8000 \
  --output none

echo "==> Startup command, websockets, Always On"
az webapp config set \
  --name "$APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --startup-file "startup.sh" \
  --web-sockets-enabled true \
  --always-on true \
  --output none

URL="https://$(az webapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --query defaultHostName -o tsv)"
echo "==> Infrastructure ready. App URL: $URL"
echo "    Next: set up the GitHub OIDC identity (README > Deploy > CI/CD),"
echo "    then push to main to deploy."
