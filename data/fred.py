"""FRED-backed fetcher: EM Sovereign Bond Spread (ICE BofA, BAMEMBBSOAS).

Uses ``fredapi``. The API key is resolved from Streamlit secrets or the
environment via :func:`config.get_fred_api_key`; when absent the fetcher
returns a clean failure so the rest of the dashboard still renders (FR-06).
"""

from __future__ import annotations

import pandas as pd

from config import get_fred_api_key
from data.base import FetchResult, utcnow, with_retry


@with_retry
def _fetch_series(api_key: str, series_id: str) -> pd.Series:
    from fredapi import Fred

    fred = Fred(api_key=api_key)
    series = fred.get_series(series_id)
    if series is None or series.dropna().empty:
        raise ValueError(f"FRED returned no data for {series_id}")
    return series.dropna()


def fetch_em_spread(label: str, series_id: str, lookback_days: int) -> FetchResult:
    """EM sovereign spread time series trimmed to ``lookback_days`` (FR-03)."""
    api_key = get_fred_api_key()
    if not api_key:
        return FetchResult.failure("fred", label, "FRED_API_KEY not set")
    try:
        series = _fetch_series(api_key, series_id)
        series.index = pd.to_datetime(series.index)
        # ICE BofA OAS series are quoted in percentage points; convert to
        # basis points so the value matches the "bps" unit and bps threshold.
        series = series * 100.0
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
        return FetchResult.failure("fred", label, str(exc))
