"""Offline tests for yfinance-backed data fetchers."""

from __future__ import annotations

import pandas as pd

from data import yf


def test_short_sp500_lookback_has_ma200(monkeypatch):
    def fake_download(symbol: str, *, period: str, **kwargs) -> pd.DataFrame:
        calendar_days = int(period.removesuffix("d"))
        end = pd.Timestamp("2026-07-17")
        index = pd.bdate_range(end=end, periods=calendar_days * 5 // 7)
        return pd.DataFrame({"Close": range(len(index))}, index=index)

    monkeypatch.setattr(yf.yf, "download", fake_download)

    result = yf.fetch_sp500("S&P 500", "^GSPC", lookback_days=30)

    assert result.ok
    assert result.series["ma200"].notna().all()
