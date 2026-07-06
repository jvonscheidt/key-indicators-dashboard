# Market Indicators Live Dashboard

A single-page Streamlit dashboard giving near-real-time visibility into key
macro, volatility, and sentiment indicators for personal investment
monitoring. Solo use, minimal infrastructure, zero recurring cost.

Out of scope: portfolio tracking, trade execution, PnL, authentication, and
any persistent storage beyond the session cache.

Code comments cite the requirement IDs used throughout this file — `FR-06`,
`NFR-02`, `§8`, `Risks §9` — so searching for an ID here finds its context.

## Indicators (8)

| Indicator | Symbol / Series | Source | Frequency | Display | Alert |
|-----------|-----------------|--------|-----------|---------|-------|
| VIX | `^VIX` | yfinance | Near-RT | Metric tile + sparkline | > 30 |
| DXY (USD Index) | `DX-Y.NYB` | yfinance | Near-RT | Metric tile + sparkline | — |
| EUR/USD | `EURUSD=X` | yfinance | Near-RT | Metric tile + sparkline | — |
| Brent Crude Spot | `DCOILBRENTEU` | FRED API | Daily | Metric tile + sparkline | — |
| S&P 500 vs 200-day MA | `^GSPC` | yfinance | Daily | Dual-line (price + MA) | — |
| Shiller CAPE | multpl.com | HTML scrape | Monthly | Value + historical line | > 35 |
| Put/Call Ratio (CBOE Total) | CBOE daily stats page | HTML scrape | Daily (EOD) | Bar chart + 10-day avg | > 1.0 |
| EM Corporate Bond Spread | `BAMLEMCBPIOAS` | FRED API | Daily | Area chart + threshold line | > 500 bps |

This is `§3` in the data layer's code comments. Alert thresholds (`FR-05`)
are configured in `config.py` and can be overridden live from the sidebar.

> **Put/Call note:** CBOE retired the public CSV feed, so `fetch_putcall`
> scrapes the total ratio out of the daily market-statistics page's
> server-rendered payload (`PUTCALL_URL` / `PUTCALL_RATIO_LABEL`). The page
> has no bulk history feed, so the recent window (capped at
> `PUTCALL_HISTORY_DAYS`) is backfilled by querying each weekday's `?dt=`
> page concurrently and de-duplicating by the page's own trade date
> (`Risks §9`: no interpolation). Each day is a full-page fetch, so the
> window is kept modest and cached for `TTL_SCRAPE` (1 h).

## Features

| ID | Requirement |
|----|-------------|
| FR-01 | Loads all indicators on startup with the latest available data. |
| FR-02 | Each tile shows current value, previous-close delta (abs + %), and a 90-day sparkline. |
| FR-03 | Global lookback selector: 1M / 3M / 6M / 1Y / 5Y. |
| FR-04 | Auto-refresh on a configurable interval (default 15 min) plus a manual refresh button. |
| FR-05 | Per-indicator threshold alerts, configured in `config.py`, overridable in the sidebar. |
| FR-06 | Graceful degradation: one failed source shows an error badge; the rest still render. |
| FR-07 | Sidebar shows per-source data-freshness timestamps. |

| ID | Non-functional requirement |
|----|----------------------------|
| NFR-01 | Full render < 10 s cold, < 2 s cached. |
| NFR-02 | `st.cache_data` TTL per source: yfinance 15 min, FRED 6 h, scrapers 1 h. |
| NFR-03 | 3 retries with exponential backoff; fall back to the last good value on failure. |
| NFR-04 | Runs on Python 3.11+ / any OS; no compiled dependencies. |
| NFR-05 | Tickers, series IDs, thresholds, and scrape selectors all externalised in `config.py`. |

## Architecture & layout

Data fetching is isolated in `data/`, one module per source and
Streamlit-agnostic; `app.py` is the UI plus the cached `load()` dispatcher
that applies the per-source TTLs (`NFR-02`).

```
config.py          # All tickers, series IDs, TTLs, thresholds, scrape selectors, FRED key resolver
data/
  base.py          # FetchResult contract + with_retry decorator + utcnow
  yf.py            # yfinance fetchers: fetch_price, fetch_sp500
  fred.py          # Generic FRED fetcher: fetch_fred (EM spread, Brent spot)
  scrape.py        # scrapers: fetch_cape (multpl.com), fetch_putcall (CBOE)
app.py             # Streamlit UI: cached load() dispatcher + §8 layout
startup.sh         # App Service launch command (Streamlit on $PORT)
scripts/
  azure-provision.sh   # one-time az CLI infra provisioning
.github/workflows/
  ci.yml                                 # ruff format --check + ruff check + pytest, on push & PRs
  main_market-indicators-dashboard.yml   # Portal-managed: build + deploy on push to main
.streamlit/
  config.toml      # committed prod settings (headless, theme, no usage stats)
tests/
  fixtures/        # captured CAPE + Put/Call page HTML samples
  test_scrape.py   # offline unit tests for both scrapers (Risks §9)
  test_app.py      # offline tests: formatting helpers (FR-02) + stale fallback
smoke_m1.py        # Throwaway verification: exercises the yfinance + FRED fetchers
requirements.txt   # pinned direct dependencies
```

Local-only (gitignored, not deployed):

```
.streamlit/secrets.toml   # FRED_API_KEY (on Azure this is an App Setting)
```

**The `§8` UI layout:** Streamlit wide mode. Top row — four `st.metric`
tiles with sparklines (VIX, DXY, EUR/USD, Brent). Second row — S&P 500 vs
200-day MA chart | Shiller CAPE value + historical line. Third row —
Put/Call bar chart | EM spread area chart. Sidebar — lookback selector,
manual + auto refresh, threshold overrides, data-freshness timestamps.

Every fetcher returns a uniform `FetchResult` (value, previous, series,
timestamp, ok/error, stale) so the UI renders success, error badges
(`FR-06`), and freshness signals (`FR-07`) the same way for all sources.
`timestamp` is the as-of date of the data itself (a trading day / series
date); `fetched_at` is the wall-clock UTC time the fetcher last retrieved
it, which is what the sidebar's "Data freshness" caption shows. When a
refresh fails, `app.load()` falls back to the session's last good result
marked `stale` (`NFR-03`) and the tile shows an amber staleness badge
(`Risks §9`); a source that has never succeeded still gets the error badge.
Failures are not memoized by `st.cache_data` (the cached loaders raise), so
a recovered source comes back on the next rerun instead of after the source
TTL; retries of a still-down source are throttled to one per
`FAILURE_RETRY_SECONDS` (60 s), and "Refresh now" bypasses the throttle.

## Tech stack

- **Python** 3.11+ (venv at `.venv/` — invoke its interpreter directly; see `CLAUDE.md`)
- **UI / charting:** Streamlit 1.58, Plotly 6.8
- **Data:** yfinance 1.5, fredapi 0.5, requests + beautifulsoup4 (scrapers)
- **Resilience:** tenacity 9.1 (retry/backoff)
- **Tests / tooling:** pytest 9, ruff 0.15 (format + lint, enforced in CI)

## Setup

```bash
.venv/bin/pip install -r requirements.txt
```

The EM-spread and Brent tiles (both FRED-sourced) need a free
[FRED API key](https://fred.stlouisfed.org/docs/api/api_key.html), read from
`FRED_API_KEY` (environment variable or `.streamlit/secrets.toml`). Without
it those two tiles degrade gracefully; the rest of the dashboard works.

## Running

```bash
.venv/bin/python smoke_m1.py     # verify yfinance + FRED fetchers
.venv/bin/streamlit run app.py   # launch the dashboard
.venv/bin/pytest                 # run the unit tests
```

## Deploy (Azure App Service)

The dashboard is live on Azure App Service (a deliberate deviation from the
original Streamlit Community Cloud target).

**Infrastructure** (resource group, Linux Python App Service plan/webapp,
`FRED_API_KEY` app setting, Oryx build flag, websockets, Always On,
`startup.sh` as the launch command) is provisioned by
`scripts/azure-provision.sh`, idempotent — re-run to update:

```bash
az login
FRED_API_KEY=xxxx ./scripts/azure-provision.sh
# override defaults as needed:
APP_NAME=my-unique-name LOCATION=westeurope SKU=B1 \
  FRED_API_KEY=xxxx ./scripts/azure-provision.sh
```

`APP_NAME` must be globally unique. The FRED key comes from the environment,
so `.streamlit/secrets.toml` is never deployed.

**CI/CD** is wired through the Azure Portal's Deployment Center (OIDC /
federated credentials, not a publish-profile secret), which generated and
owns `.github/workflows/main_market-indicators-dashboard.yml`. On push to
`main` it builds, then deploys to the `market-indicators-dashboard` App
Service via `azure/webapps-deploy`. Format, lint, and test gating lives in
the separate `ci.yml` workflow, which also runs on pull requests.

> **Careful:** because the Portal manages the deploy workflow file, re-running
> its Deployment Center setup wizard can silently overwrite manual edits
> (including the pinned action versions there). If you reconfigure deployment
> from the Portal, diff the resulting file against git afterward.

## Risks & mitigations (`Risks §9`)

| Risk | Impact | Mitigation |
|------|--------|------------|
| Scrape breakage | multpl.com or CBOE changes HTML structure. | Pin selectors in `config.py`; a unit test per scraper; surface parse failures as an error badge. |
| Rate limiting | yfinance or FRED throttles requests. | Cache aggressively; respect TTLs; retry with backoff. |
| Data gaps | CAPE updates monthly; CBOE P/C is EOD only. | Show a staleness badge; never interpolate. |

## License

GPL-2.0-or-later. See [`LICENSE`](LICENSE).
