#!/usr/bin/env bash
#
# Provision the Market Indicators dashboard infrastructure on Azure App
# Service (Linux, Python). One-time / idempotent — re-running updates the
# existing resources. Ongoing code deploys are handled by GitHub Actions via
# the Azure Portal's Deployment Center (OIDC), which owns
# .github/workflows/main_market-indicators-dashboard.yml — not this script.
#
# Prerequisites:
#   - Azure CLI installed and logged in:  az login
#   - FRED_API_KEY exported in your shell (it is set as an App Service
#     Application Setting; config.get_fred_api_key() reads it from the env).
#
# Usage:
#   FRED_API_KEY=xxxx ./scripts/azure-provision.sh
#   APP_NAME=my-unique-name LOCATION=westeurope ./scripts/azure-provision.sh
#
# After it finishes: connect this app to the GitHub repo via Portal > the
# app > Deployment Center (GitHub Actions, OIDC) using APP_NAME as the app
# name. That wizard generates/updates the deploy workflow and its secrets —
# see the "Careful" note in README.md's Deploy section before re-running it.
#
set -euo pipefail

# --- Configuration (override any of these via environment variables) --------
RESOURCE_GROUP="${RESOURCE_GROUP:-market-dashboard-rg}"
LOCATION="${LOCATION:-westeurope}"
PLAN_NAME="${PLAN_NAME:-market-dashboard-plan}"
SKU="${SKU:-B1}"                          # B1 supports Always On + websockets
APP_NAME="${APP_NAME:-market-indicators-dashboard}"   # must be globally unique
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
echo "    Next: Portal > $APP_NAME > Deployment Center > connect this GitHub"
echo "    repo (OIDC). Push to main to deploy."
