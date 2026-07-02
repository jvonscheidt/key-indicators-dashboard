"""HTML scrapers: Shiller CAPE (multpl.com) and CBOE Put/Call ratio.

Both return a :class:`FetchResult` so the UI treats them like every other
source. Per Risks §9 the URLs / selectors are pinned in ``config.py`` (a site
change is a one-line config fix), and any parse failure degrades to a clean
:class:`FetchResult.failure` (FR-06) instead of crashing the dashboard. The
HTTP fetch is wrapped in the shared retry/backoff policy (NFR-03).
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pandas as pd
import requests
from bs4 import BeautifulSoup

from config import (
    CAPE_TABLE_ID,
    CAPE_TABLE_URL,
    PUTCALL_HISTORY_DAYS,
    PUTCALL_MAX_WORKERS,
    PUTCALL_RATIO_LABEL,
    PUTCALL_URL,
)
from data.base import FetchResult, utcnow, with_retry

#: A browser-ish UA avoids the bare-``python-requests`` blocks some sites apply.
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MarketDashboard/1.0)"}
_HTTP_TIMEOUT = 20


@with_retry
def _get(url: str) -> str:
    """GET ``url`` and return the body text, raising on any HTTP error.

    Raising lets the shared :func:`with_retry` policy engage on transient
    failures; the caller converts a final failure into a ``FetchResult``.
    """
    resp = requests.get(url, headers=_HEADERS, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.text


# --------------------------------------------------------------------------
# Shiller CAPE (multpl.com)
# --------------------------------------------------------------------------


def _parse_cape_table(html: str) -> pd.Series:
    """Parse multpl's month-by-month table into a date-indexed value series.

    Parsed by hand with BeautifulSoup rather than ``pandas.read_html`` so the
    only scraping dependency is bs4 (NFR-04 — no lxml/html5lib).
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id=CAPE_TABLE_ID) or soup.find("table")
    if table is None:
        raise ValueError("CAPE history table not found")

    dates: list[str] = []
    values: list[float] = []
    for row in table.find_all("tr"):
        cells = [c.get_text(strip=True) for c in row.find_all("td")]
        if len(cells) < 2:
            continue  # header / spacer rows have no <td>s
        try:
            values.append(float(cells[1].replace(",", "")))
        except ValueError:
            continue  # an estimate footnote or stray row; skip it
        dates.append(cells[0])

    if not values:
        raise ValueError("no CAPE rows parsed (selector may be stale)")
    series = pd.Series(values, index=pd.to_datetime(dates), name="value")
    return series.sort_index()


def fetch_cape(label: str, lookback_days: int) -> FetchResult:
    """Shiller CAPE: latest value plus monthly history (multpl.com scrape).

    CAPE is monthly, so a short ``lookback_days`` may not span two points;
    the series is never interpolated (Risks §9) but we keep at least the last
    two observations so the tile can still show a delta and a sparkline.
    """
    try:
        series = _parse_cape_table(_get(CAPE_TABLE_URL))
        cutoff = series.index.max() - pd.Timedelta(days=lookback_days)
        trimmed = series[series.index >= cutoff]
        if len(trimmed) < 2:
            trimmed = series.tail(2)
        value = float(series.iloc[-1])
        previous = float(series.iloc[-2]) if len(series) > 1 else None
        return FetchResult(
            source="scrape",
            label=label,
            value=value,
            previous=previous,
            series=trimmed.to_frame(),
            timestamp=series.index[-1].to_pydatetime(),
            fetched_at=utcnow(),
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a failed tile
        return FetchResult.failure("scrape", label, str(exc))


# --------------------------------------------------------------------------
# CBOE Equity Put/Call ratio
# --------------------------------------------------------------------------


def _parse_putcall_page(html: str, row_label: str) -> tuple[float, pd.Timestamp | None]:
    """Pull one put/call ratio + the trade date from CBOE's daily page.

    The page is a Next.js app whose server-rendered payload embeds the day's
    ratios as ``{"name": "...", "value": "..."}`` objects (quotes escaped as
    ``\\"``), plus a ``"selectedDate"`` trade date. We read those out of the
    raw HTML directly — no JS execution or extra dependency required.
    """
    value_re = re.compile(
        r'name\\?"\s*:\s*\\?"' + re.escape(row_label) + r'\\?"\s*,\s*\\?"value\\?"\s*:\s*\\?"([0-9.]+)\\?"'
    )
    match = value_re.search(html)
    if match is None:
        raise ValueError(f"{row_label!r} not found on page (layout may have changed)")
    value = float(match.group(1))

    date_match = re.search(r'selectedDate\\?"\s*:\s*\\?"(\d{4}-\d{2}-\d{2})\\?"', html)
    trade_date = pd.Timestamp(date_match.group(1)) if date_match else None
    return value, trade_date


def _fetch_putcall_day(date_str: str) -> tuple[pd.Timestamp, float] | None:
    """Fetch one trading day via ``?dt=``; return (trade_date, value) or None.

    A single bad/missing day must not sink the whole series, so failures are
    swallowed here and simply dropped by the caller.
    """
    try:
        value, trade_date = _parse_putcall_page(
            _get(f"{PUTCALL_URL}?dt={date_str}"), PUTCALL_RATIO_LABEL
        )
    except Exception:  # noqa: BLE001 - one day failing is non-fatal
        return None
    if trade_date is None:
        return None
    return trade_date, value


def fetch_putcall(label: str, lookback_days: int) -> FetchResult:
    """CBOE equity put/call ratio with a backfilled daily history.

    The page has no bulk history feed, so the recent window (capped at
    ``PUTCALL_HISTORY_DAYS``) is reconstructed by querying each weekday's
    ``?dt=`` page concurrently. Non-trading days resolve to the prior trading
    day and are de-duplicated by the page's own trade date — the series is
    never interpolated (Risks §9).
    """
    try:
        window = min(lookback_days, PUTCALL_HISTORY_DAYS)
        today = utcnow().date()
        weekdays = [
            (today - timedelta(days=n)).isoformat()
            for n in range(window + 1)
            if (today - timedelta(days=n)).weekday() < 5
        ]
        with ThreadPoolExecutor(max_workers=PUTCALL_MAX_WORKERS) as pool:
            days = [d for d in pool.map(_fetch_putcall_day, weekdays) if d is not None]
        if not days:
            return FetchResult.failure("scrape", label, "no put/call data scraped")

        # Dedupe by trade date (holidays collapse onto the prior trading day).
        by_date = {ts: val for ts, val in days}
        series = pd.Series(by_date, name="value").sort_index().to_frame()
        value = float(series["value"].iloc[-1])
        previous = float(series["value"].iloc[-2]) if len(series) > 1 else None
        return FetchResult(
            source="scrape",
            label=label,
            value=value,
            previous=previous,
            series=series,
            timestamp=series.index[-1].to_pydatetime(),
            fetched_at=utcnow(),
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a failed tile
        return FetchResult.failure("scrape", label, str(exc))
