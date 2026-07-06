"""Unit tests for app.py's pure formatting helpers (no Streamlit runtime).

The threshold/alert helpers read ``st.session_state`` and are exercised via
the AppTest harness instead; these cover the value/delta formatting that the
metric tiles depend on (FR-02).
"""

from __future__ import annotations

from datetime import datetime, timezone

import app
from data.base import FetchResult


def test_fmt_value_per_unit():
    assert app.fmt_value("eurusd", 1.15274) == "1.1527"  # FX: 4 dp
    assert app.fmt_value("em_spread", 144.0) == "144 bps"  # bps: integer + unit
    assert app.fmt_value("vix", 21.5) == "21.50"  # default: 2 dp
    assert app.fmt_value("vix", None) == "—"  # missing value


def test_fmt_delta_includes_pct():
    res = FetchResult(source="yfinance", label="VIX", value=22.0, previous=20.0)
    assert app.fmt_delta("vix", res) == "+2.00 (+10.00%)"


def test_fmt_delta_fx_precision():
    res = FetchResult(source="yfinance", label="EUR/USD", value=1.1527, previous=1.1609)
    out = app.fmt_delta("eurusd", res)
    assert out.startswith("-0.0082")  # FX deltas shown to 4 dp


def test_fmt_delta_none_when_no_previous():
    res = FetchResult(source="scrape", label="CAPE", value=41.0, previous=None)
    assert app.fmt_delta("cape", res) is None


# --------------------------------------------------------------------------
# Last-good / stale fallback (NFR-03, Risks §9)
# --------------------------------------------------------------------------


def test_with_last_good_records_success():
    store: dict[str, FetchResult] = {}
    good = FetchResult(source="scrape", label="CAPE", value=41.0)
    assert app.with_last_good(good, store, "cape") is good
    assert store["cape"] is good


def test_with_last_good_serves_stale_on_failure():
    fetched_at = datetime(2026, 7, 2, 9, 30, tzinfo=timezone.utc)
    good = FetchResult(
        source="scrape", label="CAPE", value=41.0, previous=40.5, fetched_at=fetched_at
    )
    store = {"cape": good}
    fail = FetchResult.failure("scrape", "CAPE", "boom")

    out = app.with_last_good(fail, store, "cape")

    assert out.ok and out.stale  # renders as a normal tile + stale badge
    assert out.value == 41.0
    assert out.error == "boom"  # fresh error carried for the badge tooltip
    assert out.fetched_at == fetched_at  # freshness caption shows last real fetch
    assert store["cape"] is good  # failure does not overwrite the last good
    assert not good.stale  # stored copy is untouched


def test_with_last_good_failure_without_history_passes_through():
    fail = FetchResult.failure("scrape", "CAPE", "boom")
    out = app.with_last_good(fail, {}, "cape")
    assert out is fail
    assert not out.ok and not out.stale  # still an error badge (FR-06)
