#!/usr/bin/env bash
# App Service startup command (set via `az webapp config set --startup-file`).
# Oryx installs requirements.txt during build; here we just launch Streamlit,
# binding to the port App Service routes to (PORT, default 8000) on all
# interfaces so the platform health check and proxy can reach it.
set -euo pipefail

exec python -m streamlit run app.py \
  --server.port "${PORT:-8000}" \
  --server.address 0.0.0.0 \
  --server.headless true \
  --browser.gatherUsageStats false
