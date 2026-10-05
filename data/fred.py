"""Generic FRED-backed fetcher (FRED ``series/observations`` API).

Fetches any FRED series by ID and trims it to the requested lookback window.
The API key is resolved from Streamlit secrets or the environment via
:func:`config.get_fred_api_key`; when absent the fetcher returns a clean
failure so the rest of the dashboard still renders (FR-06).

The API is called with ``requests`` rather than ``fredapi``: fredapi opens
connections with no timeout, so a stalled FRED request could block the page
indefinitely (NFR-03).

Series that are quoted in units the dashboard wants to display differently
(e.g. ICE BofA OAS series are in percentage points but shown in basis
points) pass a ``scale`` multiplier — kept out of this fetcher so it stays
series-agnostic; the per-indicator value lives in ``config.Indicator``.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pandas as pd
import requests

from config import (
    FRED_OBSERVATIONS_URL,
    FRED_START_SLACK_DAYS,
    HTTP_TIMEOUT_SECONDS,
    get_fred_api_key,
)
from data.base import FetchResult, utcnow, with_retry


@with_retry
def _fetch_series(api_key: str, series_id: str, start: date) -> pd.Series:
    resp = requests.get(
        FRED_OBSERVATIONS_URL,
        params={
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": start.isoformat(),
        },
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    observations = resp.json().get("observations", [])
    # FRED reports a missing observation as the string ".".
    series = pd.to_numeric(
        pd.Series({obs["date"]: obs["value"] for obs in observations}, dtype=object),
        errors="coerce",
    ).dropna()
    if series.empty:
        raise ValueError(f"FRED returned no data for {series_id}")
    series.index = pd.to_datetime(series.index)
    return series


def _redact(message: str, api_key: str) -> str:
    """Remove the API key from an error message before it reaches the UI.

    requests puts the full request URL, query string included, into HTTP and
    connection errors, and those messages are shown on a public tile.
    """
    message = message.replace(api_key, "***")
    return re.sub(r"api_key=[^&\s'\"]*", "api_key=***", message)


def fetch_fred(
    label: str, series_id: str, lookback_days: int, scale: float = 1.0
) -> FetchResult:
    """Fetch a FRED series trimmed to ``lookback_days`` (FR-03).

    ``scale`` multiplies every observation before trimming (defaults to no
    scaling). The EM-spread indicator uses ``scale=100.0`` to convert ICE
    BofA percentage points into the basis points its tile displays.
    """
    api_key = get_fred_api_key()
    if not api_key:
        return FetchResult.failure("fred", label, "FRED_API_KEY not set")
    try:
        start = utcnow().date() - timedelta(days=lookback_days + FRED_START_SLACK_DAYS)
        series = _fetch_series(api_key, series_id, start)
        if scale != 1.0:
            series = series * scale
        cutoff = series.index.max() - pd.Timedelta(days=lookback_days)
        trimmed = series[series.index >= cutoff].rename("value")
        if trimmed.empty:
            return FetchResult.failure("fred", label, "empty series in window")
        value = float(trimmed.iloc[-1])
        previous = float(trimmed.iloc[-2]) if len(trimmed) > 1 else None
        return FetchResult(
            source="fred",
            label=label,
            value=value,
            previous=previous,
            series=trimmed.to_frame(),
            timestamp=trimmed.index[-1].to_pydatetime(),
            fetched_at=utcnow(),
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a failed tile
        return FetchResult.failure("fred", label, _redact(str(exc), api_key))
