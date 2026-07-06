# Market Indicators Live Dashboard

A single-page Streamlit dashboard giving near-real-time visibility into key
macro, volatility, and sentiment indicators for personal investment
monitoring. Solo use, minimal infrastructure, zero recurring cost.

This file is both the spec and the build-state summary: the authoritative
requirements (v1.0, 2026-06-07) are preserved verbatim in
[Requirements](#requirements-v10--2026-06-07) below — code comments cite
their section numbers (`§8`, `Risks §9`) and IDs (`FR-06`, `NFR-02`). 

## Indicators (7)

| Indicator | Symbol / Series | Source | Status |
|-----------|-----------------|--------|--------|
| VIX | `^VIX` | yfinance | ✅ fetcher |
| DXY (USD Index) | `DX-Y.NYB` | yfinance | ✅ fetcher |
| EUR/USD | `EURUSD=X` | yfinance | ✅ fetcher |
| S&P 500 vs 200-day MA | `^GSPC` | yfinance | ✅ fetcher (MA overlay) |
| EM Corporate Bond Spread | `BAMLEMCBPIOAS` | FRED API | ✅ fetcher |
| Shiller CAPE | multpl.com | HTML scrape | ✅ fetcher |
| Put/Call Ratio | CBOE daily stats page | HTML scrape | ✅ fetcher (backfilled history) |

Alert thresholds (FR-05) are configured for VIX (>30), CAPE (>35),
Put/Call (>1.0), and EM spread (>500 bps) in `config.py`.

> **Put/Call note:** CBOE retired the public CSV feed, so `fetch_putcall`
> now scrapes the total ratio out of the daily market-statistics page's
> server-rendered payload (`PUTCALL_URL` / `PUTCALL_RATIO_LABEL`). The page
> has no bulk history feed, so the recent window (capped at
> `PUTCALL_HISTORY_DAYS`) is backfilled by querying each weekday's `?dt=`
> page concurrently and de-duplicating by the page's own trade date (Risks
> §9: no interpolation). Each day is a full-page fetch, so the window is kept
> modest and cached for `TTL_SCRAPE` (1 h).

## Tech stack

- **Python** 3.11+ (venv at `.venv/` — invoke its interpreter directly; see `CLAUDE.md`)
- **UI:** Streamlit 1.58
- **Charting:** Plotly 6.8
- **Data:** yfinance 1.4, fredapi 0.5, requests + beautifulsoup4 (scrapers)
- **Resilience:** tenacity 9.1 (retry/backoff)
- **Tests:** pytest 9

## Layout

Files that exist today:

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
  main_market-indicators-dashboard.yml   # Portal-managed: test + build + deploy on push to main
.streamlit/
  config.toml      # committed prod settings (headless, theme, no usage stats)
tests/
  fixtures/        # captured CAPE + Put/Call page HTML samples
  test_scrape.py   # offline unit tests for both scrapers (Risks §9)
  test_app.py      # offline tests: formatting helpers (FR-02) + stale fallback
smoke_m1.py        # Throwaway M1 verification: exercises yfinance + FRED fetchers
requirements.txt   # pinned direct dependencies
```

Local-only (gitignored, not deployed):

```
.streamlit/secrets.toml   # FRED_API_KEY (on Azure this is an App Setting)
```

Every fetcher returns a uniform `FetchResult` (value, previous, series,
timestamp, ok/error, stale) so the UI can render success, error badges
(FR-06), and freshness signals (FR-07) the same way for all sources.
`timestamp` is the as-of date of the data itself (a trading day / series
date), which is date-only by nature; `fetched_at` is the wall-clock UTC
time the fetcher last actually retrieved it, which is what the sidebar's
"Data freshness" caption shows.
When a refresh fails, `app.load()` falls back to the session's last good
result marked `stale` (NFR-03) and the tile shows an amber staleness badge
(Risks §9); a source that has never succeeded still gets the error badge.
Failures are not memoized by `st.cache_data` (the cached loaders raise), so
a recovered source comes back on the next rerun instead of after the source
TTL; retries of a still-down source are throttled to one per
`FAILURE_RETRY_SECONDS` (60 s) and "Refresh now" bypasses the throttle.

## Setup

```bash
.venv/bin/pip install -r requirements.txt
```

The EM-spread tile needs a free [FRED API key](https://fred.stlouisfed.org/docs/api/api_key.html),
read from `FRED_API_KEY` (environment variable or `.streamlit/secrets.toml`).
Without it that one tile degrades gracefully; the rest of the dashboard works.

## Running

```bash
.venv/bin/python smoke_m1.py     # verify yfinance + FRED fetchers
.venv/bin/streamlit run app.py   # launch the dashboard
.venv/bin/pytest                 # run the unit tests
```

## Current state (as of 2026-07-02)

Milestones 1–4 are complete. The data layer (yfinance + FRED + scrapers),
the offline tests, and the full Streamlit UI (`app.py`) all exist and run
end-to-end. `app.py` implements the §8 layout — three metric tiles with
sparklines, the S&P/MA and CAPE panels, the Put/Call and EM panels — plus
the sidebar (lookback selector, manual + auto refresh, threshold overrides,
freshness timestamps). `st.cache_data` TTLs are applied per source via the
`load()` dispatcher (NFR-02); threshold alerts, per-tile graceful
degradation, and the stale last-good fallback work (FR-05/06, NFR-03).
Deploy is wired up and live on Azure App Service (deviates from the spec's
Streamlit Community Cloud target).

## Deploy (Azure App Service)

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
`main` it runs the tests, builds, then deploys to the `market-indicators-dashboard`
App Service via `azure/webapps-deploy`.

> **Careful:** because the Portal manages this file, re-running its
> Deployment Center setup wizard can silently overwrite manual edits
> (including the test gate and pinned action versions here). If you need to
> reconfigure deployment from the Portal, diff the resulting file against
> git afterward.

---

## Requirements (v1.0 — 2026-06-07)

The original condensed requirements document, preserved as written (the
build-state sections above flag where the implementation deviates). Code
comments reference the section numbers and requirement IDs below.

### §1 Purpose

Single-page Streamlit dashboard providing near-real-time visibility into key
macro, volatility, and sentiment indicators for personal investment
monitoring. Designed for solo use, minimal infrastructure, and zero
recurring cost.

### §2 Scope

**In scope**

- Seven core indicators (see §3) with current value, trend sparkline, and
  configurable alert thresholds.
- Auto-refresh on a configurable interval (default: 15 min during market
  hours).
- Deployable via Azure App Services or similar cloud service

**Out of scope**

- Portfolio tracking, trade execution, or PnL calculation.
- User authentication or multi-tenancy.
- Persistent storage / historical database beyond session cache.

### §3 Indicators & data sources

| Indicator | Ticker / Series | Source | Frequency | Display |
|-----------|-----------------|--------|-----------|---------|
| VIX | `^VIX` | yfinance | Near-RT | Gauge + 30-day line |
| DXY (USD Index) | `DX-Y.NYB` | yfinance | Near-RT | Line chart + Δ% |
| Shiller CAPE (S&P 500) | CAPE via Shiller site | HTML scrape (multpl.com) | Monthly | Single value + hist. line |
| S&P 500 vs 200-day MA | `^GSPC` | yfinance | Daily | Dual-line (price + MA) |
| EUR/USD | `EURUSD=X` | yfinance | Near-RT | Line chart + Δ% |
| Put/Call Ratio (CBOE Total) | CBOE totals page | HTML scrape (cboe.com) | Daily (EOD) | Bar chart + 10-day avg line |
| EM Sovereign Bond Spread | BAMEMBBSOAS (ICE BofA) | FRED API | Daily | Area chart + threshold line |

### §4 Technology stack

| Layer | Technology |
|-------|------------|
| Language | Python 3.11+ |
| UI framework | Streamlit (latest stable) |
| Charting | Plotly Express / Graph Objects |
| Market data | yfinance |
| Macro data | fredapi (FRED API, free key) |
| Scraping | requests + BeautifulSoup4 |

### §5 Architecture overview

Single-file Streamlit app (`app.py`) with modular helper functions. Data
fetching is isolated into a `data/` module with one function per source
(yfinance, FRED, scraper). Streamlit's `st.cache_data` with TTL handles
caching; no external DB required.

- `app.py` — layout, widgets, refresh logic.
- `data/yf.py` — yfinance wrappers (VIX, DXY, EUR/USD, S&P 500).
- `data/fred.py` — FRED API wrapper (EM spread).
- `data/scrape.py` — HTML scrapers (Shiller CAPE, CBOE Put/Call).
- `config.py` — thresholds, refresh interval, FRED API key, display params.
- `requirements.txt` — pinned dependencies.

### §6 Functional requirements

| ID | Requirement |
|----|-------------|
| FR-01 | Dashboard loads all seven indicators on startup with latest available data. |
| FR-02 | Each indicator tile shows: current value, previous close delta (abs + %), and a sparkline (default 90 days). |
| FR-03 | User can select lookback period globally: 1M / 3M / 6M / 1Y / 5Y. |
| FR-04 | Auto-refresh via `st.rerun` on configurable interval; manual refresh button available. |
| FR-05 | Threshold alerts: configurable per indicator (e.g., VIX > 30 → red highlight). Defined in `config.py`. |
| FR-06 | Graceful degradation: if a single data source fails, remaining indicators still render; failed tile shows error badge. |
| FR-07 | Sidebar displays data freshness timestamps per source. |

### §7 Non-functional requirements

| ID | Category | Requirement |
|----|----------|-------------|
| NFR-01 | Performance | Full dashboard render < 10 s on cold start (cached < 2 s). |
| NFR-02 | Caching | `st.cache_data` with TTL per source: yfinance 15 min, FRED 6 h, scrapers 1 h. |
| NFR-03 | Resilience | Per-source try/except with fallback to last cached value; 3 retries with exponential backoff on HTTP errors. |
| NFR-04 | Portability | Runs on Python 3.11+ / any OS. No compiled dependencies beyond standard pip packages. |
| NFR-05 | Maintainability | All tickers, series IDs, thresholds, and scrape selectors externalised in `config.py`. |

### §8 UI layout

Streamlit wide mode. Top row: three `st.metric` tiles (VIX, DXY, EUR/USD).
Second row: two-column split — left: S&P 500 vs 200-day MA chart; right:
CAPE value + historical line. Third row: two-column split — left: Put/Call
Ratio bar chart; right: EM Spread area chart. Sidebar: lookback selector,
refresh button, data freshness timestamps, threshold config expander.

### §9 Risks & mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Scrape breakage | multpl.com or CBOE changes HTML structure. | Pin CSS selectors in `config.py`; add unit test per scraper; alert on parse failure. |
| Rate limiting | yfinance or FRED throttles requests. | Cache aggressively; respect TTLs; add backoff. |
| Data gaps | Shiller CAPE updates monthly; CBOE P/C only EOD. | Show staleness badge; do not interpolate. |

### §10 Implementation milestones

| Milestone | Scope | Deliverable |
|-----------|-------|-------------|
| M1 | Scaffold & data layer | Project structure, config, yfinance + FRED wrappers working. |
| M2 | Scraping layer | CAPE + Put/Call scrapers with fallback logic. |
| M3 | UI & charting | Full Streamlit layout with Plotly charts, metrics, sidebar. |
| M4 | Polish & deploy | Threshold alerts, error badges, caching tuned, deploy to Azure. |

## License

GPL-2.0-or-later. See [`LICENSE`](LICENSE).
