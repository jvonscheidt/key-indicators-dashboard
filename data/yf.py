"""yfinance-backed fetchers: VIX, DXY, EUR/USD, and the S&P 500.

Each public function returns a :class:`FetchResult`. History length is
driven by the caller-supplied ``lookback_days`` (FR-03); the S&P fetcher
additionally computes a 200-day moving average overlay (§3).
"""

from __future__ import annotations

import pandas as pd
import yfinance as yf

from config import SP500_MA_WINDOW
from data.base import FetchResult, utcnow, with_retry


@with_retry
def _download(symbol: str, period_days: int, ma_lead_in: int = 0) -> pd.DataFrame:
    """Fetch daily OHLC history for ``symbol`` over the last ``period_days``.

    ``ma_lead_in`` pads the window with extra trailing days so a moving
    average still has lead-in data on short lookbacks; pass 0 (the default)
    for plain price fetches that need no overlay.

    Raises on empty results so the retry/backoff policy can engage.
    """
    fetch_days = period_days + ma_lead_in + (10 if ma_lead_in else 0)
    df = yf.download(
        symbol,
        period=f"{fetch_days}d",
        interval="1d",
        auto_adjust=False,
        progress=False,
    )
    if df is None or df.empty:
        raise ValueError(f"yfinance returned no data for {symbol}")
    # yfinance may return a column MultiIndex for a single ticker; flatten it.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def _close_series(df: pd.DataFrame, lookback_days: int) -> pd.Series:
    close = df["Close"].dropna()
    cutoff = close.index.max() - pd.Timedelta(days=lookback_days)
    return close[close.index >= cutoff]


def fetch_price(label: str, symbol: str, lookback_days: int) -> FetchResult:
    """Generic close-price fetcher used by VIX, DXY, and EUR/USD."""
    try:
        df = _download(symbol, lookback_days)
        close = _close_series(df, lookback_days)
        if close.empty:
            return FetchResult.failure("yfinance", label, "empty price series")
        series = close.rename("value").to_frame()
        value = float(close.iloc[-1])
        previous = float(close.iloc[-2]) if len(close) > 1 else None
        return FetchResult(
            source="yfinance",
            label=label,
            value=value,
            previous=previous,
            series=series,
            timestamp=close.index[-1].to_pydatetime(),
            fetched_at=utcnow(),
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a failed tile
        return FetchResult.failure("yfinance", label, str(exc))


def fetch_sp500(label: str, symbol: str, lookback_days: int) -> FetchResult:
    """S&P 500 close price plus a 200-day moving-average overlay (§3)."""
    try:
        df = _download(symbol, lookback_days, ma_lead_in=SP500_MA_WINDOW)
        close = df["Close"].dropna()
        ma = close.rolling(window=SP500_MA_WINDOW).mean()
        cutoff = close.index.max() - pd.Timedelta(days=lookback_days)
        series = pd.DataFrame({"price": close, "ma200": ma})
        series = series[series.index >= cutoff]
        if series["price"].dropna().empty:
            return FetchResult.failure("yfinance", label, "empty price series")
        value = float(close.iloc[-1])
        previous = float(close.iloc[-2]) if len(close) > 1 else None
        return FetchResult(
            source="yfinance",
            label=label,
            value=value,
            previous=previous,
            series=series,
            timestamp=close.index[-1].to_pydatetime(),
            fetched_at=utcnow(),
        )
    except Exception as exc:  # noqa: BLE001
        return FetchResult.failure("yfinance", label, str(exc))
