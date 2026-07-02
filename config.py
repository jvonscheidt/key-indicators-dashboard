"""Central configuration for the Market Indicators dashboard.

Everything environment- or source-specific lives here (NFR-05): tickers,
FRED series IDs, cache TTLs, alert thresholds, lookback options, and the
CSS selectors used by the scrapers. No business logic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Refresh / caching
# --------------------------------------------------------------------------

#: Auto-refresh interval for the live tiles, in seconds (default 15 min).
REFRESH_INTERVAL_SECONDS = 15 * 60

#: Cache TTLs per source, in seconds (NFR-02).
TTL_YFINANCE = 15 * 60   # 15 minutes
TTL_FRED = 6 * 60 * 60   # 6 hours
TTL_SCRAPE = 60 * 60     # 1 hour

#: Retry policy for HTTP fetches (NFR-03).
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_BASE = 1.0  # seconds; exponential: 1, 2, 4, ...

#: How long to remember a failed fetch before retrying it, in seconds.
#: Failures are deliberately not cached by st.cache_data (so a recovered
#: source comes back on the next rerun, not after the full source TTL);
#: this throttle keeps a source that is *still* down from being re-fetched
#: on every widget interaction.
FAILURE_RETRY_SECONDS = 60

# --------------------------------------------------------------------------
# Lookback periods (FR-03)
# --------------------------------------------------------------------------

#: Label -> number of calendar days. Insertion order drives the selector.
LOOKBACK_OPTIONS: dict[str, int] = {
    "1M": 30,
    "3M": 90,
    "6M": 180,
    "1Y": 365,
    "5Y": 365 * 5,
}
DEFAULT_LOOKBACK = "3M"

#: Sparkline window shown on each metric tile (FR-02).
SPARKLINE_DAYS = 90

# --------------------------------------------------------------------------
# Indicator definitions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Threshold:
    """Alert threshold for an indicator (FR-05).

    ``direction`` is "above" or "below": the alert fires when the current
    value crosses ``level`` in that direction.
    """

    level: float
    direction: str  # "above" | "below"

    def breached(self, value: float | None) -> bool:
        if value is None:
            return False
        if self.direction == "above":
            return value > self.level
        return value < self.level


@dataclass(frozen=True)
class Indicator:
    """Static metadata for one dashboard indicator."""

    key: str            # stable internal id
    label: str          # display name
    source: str         # "yfinance" | "fred" | "scrape"
    symbol: str         # ticker / series id / url key
    unit: str = ""
    threshold: Threshold | None = None


#: yfinance-backed indicators.
INDICATORS: dict[str, Indicator] = {
    "vix": Indicator(
        key="vix",
        label="VIX",
        source="yfinance",
        symbol="^VIX",
        threshold=Threshold(level=30.0, direction="above"),
    ),
    "dxy": Indicator(
        key="dxy",
        label="DXY (USD Index)",
        source="yfinance",
        symbol="DX-Y.NYB",
    ),
    "eurusd": Indicator(
        key="eurusd",
        label="EUR/USD",
        source="yfinance",
        symbol="EURUSD=X",
    ),
    "sp500": Indicator(
        key="sp500",
        label="S&P 500 vs 200-day MA",
        source="yfinance",
        symbol="^GSPC",
    ),
    "cape": Indicator(
        key="cape",
        label="Shiller CAPE (S&P 500)",
        source="scrape",
        symbol="cape",
        threshold=Threshold(level=35.0, direction="above"),
    ),
    "putcall": Indicator(
        key="putcall",
        label="Put/Call Ratio (CBOE Equity)",
        source="scrape",
        symbol="putcall",
        threshold=Threshold(level=1.0, direction="above"),
    ),
    "em_spread": Indicator(
        key="em_spread",
        label="EM Corporate Bond Spread",
        source="fred",
        # ICE BofA EM Corporate Plus Index, option-adjusted spread.
        symbol="BAMLEMCBPIOAS",
        unit="bps",
        threshold=Threshold(level=500.0, direction="above"),
    ),
}

#: Moving-average window for the S&P 500 overlay (FR / §3).
SP500_MA_WINDOW = 200

# --------------------------------------------------------------------------
# Scraper selectors (pinned here so breakage is a config fix; see Risks)
# --------------------------------------------------------------------------

CAPE_URL = "https://www.multpl.com/shiller-pe"
#: multpl.com renders the current value in <div id="current"> ... </div>.
CAPE_CURRENT_SELECTOR = "#current"
#: The month-by-month history (used for the sparkline) lives on a sub-page,
#: in a <table id="datatable"> with Date / Value columns.
CAPE_TABLE_URL = "https://www.multpl.com/shiller-pe/table/by-month"
CAPE_TABLE_ID = "datatable"

#: CBOE's daily market-statistics page embeds the day's ratios in its
#: server-rendered (Next.js RSC) payload as {"name": ..., "value": ...}
#: objects, alongside a "selectedDate" trade date. Only the current EOD
#: snapshot is published — there is no history feed, so the tile shows a
#: single point (Risks §9: no interpolation).
PUTCALL_URL = "https://www.cboe.com/markets/us/options/market-statistics/daily/"
#: The exact row name to read out of the page payload.
PUTCALL_RATIO_LABEL = "EQUITY PUT/CALL RATIO"
#: There is no bulk history feed, so the chart is backfilled one trading day
#: at a time via the page's ``?dt=YYYY-MM-DD`` parameter. Cap the window (each
#: day is a full-page fetch — kept modest for politeness/rate limits, Risks
#: §9, and cold-start budget NFR-01) and fetch the days concurrently.
PUTCALL_HISTORY_DAYS = 21
PUTCALL_MAX_WORKERS = 8

# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------


def get_fred_api_key() -> str | None:
    """Resolve the FRED API key from Streamlit secrets or the environment.

    Returns ``None`` if unset so the EM-spread tile can degrade gracefully
    instead of crashing the dashboard.
    """
    # Lazy import: this module must stay importable outside Streamlit (tests).
    try:
        import streamlit as st

        if "FRED_API_KEY" in st.secrets:
            return str(st.secrets["FRED_API_KEY"])
    except Exception:
        pass
    return os.environ.get("FRED_API_KEY")
