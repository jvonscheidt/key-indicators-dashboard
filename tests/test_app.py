"""Unit tests for app.py's helpers, run without a Streamlit runtime.

Covers value/delta formatting (FR-02), the last-good fallback, concurrent
loading, lookback trimming, data-age checks, and the refresh cooldown.
Where a helper touches ``st.session_state`` it is swapped for a plain dict.
"""

from __future__ import annotations

import threading
from collections import Counter
from datetime import date, datetime, UTC

import pandas as pd
import pytest

import app
import config
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
    fetched_at = datetime(2026, 7, 2, 9, 30, tzinfo=UTC)
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
    assert store["cape"] is good  # failure does not overwrite last good
    assert not good.stale  # stored copy is untouched


def test_with_last_good_failure_without_history_passes_through():
    fail = FetchResult.failure("scrape", "CAPE", "boom")
    out = app.with_last_good(fail, {}, "cape")
    assert out is fail
    assert not out.ok and not out.stale  # still an error badge (FR-06)


def test_with_last_good_does_not_cross_indicators():
    good = FetchResult(source="scrape", label="CAPE", value=41.0)
    store = {"cape": good}
    fail = FetchResult.failure("scrape", "Put/Call", "boom")

    out = app.with_last_good(fail, store, "putcall")

    assert out is fail
    assert not out.ok and not out.stale


# --------------------------------------------------------------------------
# Concurrent loading + failure throttle (FR-06, NFR-01)
# --------------------------------------------------------------------------


def test_load_all_fetches_sources_concurrently(monkeypatch):
    monkeypatch.setattr(app.st, "session_state", {})
    # Every fetch waits until all of them are in flight, so this only
    # completes if the sources run concurrently rather than one by one.
    barrier = threading.Barrier(len(app.INDICATORS), timeout=5)

    def fake_fetch(key):
        barrier.wait()
        return FetchResult(source="test", label=key, value=1.0)

    monkeypatch.setattr(app, "_fetch", fake_fetch)
    results = app.load_all()

    assert list(results) == list(app.INDICATORS)
    assert all(r.ok for r in results.values())


def test_load_all_throttles_failed_sources(monkeypatch):
    monkeypatch.setattr(app.st, "session_state", {})
    calls: Counter[str] = Counter()

    def fake_fetch(key):
        calls[key] += 1
        if key == "cape":
            return FetchResult.failure("scrape", "CAPE", "down")
        return FetchResult(source="test", label=key, value=1.0)

    monkeypatch.setattr(app, "_fetch", fake_fetch)
    app.load_all()
    results = app.load_all()

    assert calls["cape"] == 1  # still inside FAILURE_RETRY_SECONDS
    assert calls["vix"] == 2
    assert not results["cape"].ok
    assert results["vix"].ok


def test_default_lookback_is_an_option():
    # render_sidebar indexes LOOKBACK_OPTIONS by the default; drift crashes it.
    assert config.DEFAULT_LOOKBACK in config.LOOKBACK_OPTIONS
    assert max(config.LOOKBACK_OPTIONS.values()) == config.MAX_LOOKBACK_DAYS


# --------------------------------------------------------------------------
# Lookback trimming + data age (FR-03, Risks §9)
# --------------------------------------------------------------------------


def _daily(start: str, end: str) -> FetchResult:
    index = pd.date_range(start, end, freq="D")
    series = pd.DataFrame({"value": range(len(index))}, index=index, dtype=float)
    return FetchResult(source="test", label="x", value=1.0, series=series)


def test_for_lookback_trims_to_window_anchored_on_latest_point():
    out = app.for_lookback(_daily("2021-01-01", "2026-09-30"), 30)
    assert out.series.index.min() == pd.Timestamp("2026-08-31")
    assert out.series.index.max() == pd.Timestamp("2026-09-30")


def test_for_lookback_keeps_two_points_for_monthly_series():
    index = pd.to_datetime(["2026-08-01", "2026-09-01", "2026-10-01"])
    monthly = FetchResult(
        source="scrape",
        label="CAPE",
        value=41.0,
        series=pd.DataFrame({"value": [40.0, 40.5, 41.0]}, index=index),
    )
    out = app.for_lookback(monthly, 5)
    assert list(out.series.index) == list(index[-2:])


def test_for_lookback_passes_failures_through():
    fail = FetchResult.failure("scrape", "CAPE", "boom")
    assert app.for_lookback(fail, 30) is fail


def test_history_start_flags_short_sources_only():
    # Put/Call: about three weeks of backfill against a one-year lookback.
    assert app.history_start(_daily("2026-09-09", "2026-09-30"), 365) == pd.Timestamp(
        "2026-09-09"
    )
    # Plenty of history: no note.
    assert app.history_start(_daily("2021-01-01", "2026-09-30"), 365) is None
    # A few days short of the window (weekend at the edge) is within slack.
    assert app.history_start(_daily("2025-10-03", "2026-09-30"), 365) is None


def test_history_start_allows_a_monthly_series_its_spacing():
    # CAPE at 5Y: the first month-start falls ~4 weeks after the cutoff.
    index = pd.date_range("2021-11-01", "2026-10-01", freq="MS")
    monthly = FetchResult(
        source="scrape",
        label="CAPE",
        value=41.0,
        series=pd.DataFrame({"value": 40.0}, index=index),
    )
    assert app.history_start(monthly, 365 * 5) is None


def test_is_outdated_uses_per_indicator_age():
    result = FetchResult(
        source="fred",
        label="Brent",
        value=80.0,
        timestamp=pd.Timestamp("2026-09-29").to_pydatetime(),
    )
    today = date(2026, 10, 5)  # 6 days later
    assert app.data_age_days(result, today) == 6
    assert not app.is_outdated("brent", result, today)  # Brent allows 14 days
    assert app.is_outdated("vix", result, today)  # daily market data: 4 days


def test_is_outdated_without_timestamp_is_false():
    result = FetchResult(source="test", label="x", value=1.0)
    assert not app.is_outdated("vix", result, date(2026, 10, 5))


# --------------------------------------------------------------------------
# Manual refresh cooldown
# --------------------------------------------------------------------------


class _FakeLoader:
    def __init__(self):
        self.cleared = 0

    def clear(self):
        self.cleared += 1


@pytest.fixture
def fake_refresh(monkeypatch):
    clock = {"lock": threading.Lock(), "at": None}
    loaders = (_FakeLoader(), _FakeLoader())
    monkeypatch.setattr(app, "_refresh_clock", lambda: clock)
    monkeypatch.setattr(app, "_LOADERS", loaders)
    return clock, loaders


def test_refresh_clears_only_data_loaders(fake_refresh):
    _, loaders = fake_refresh
    assert app.request_refresh() is None
    assert [loader.cleared for loader in loaders] == [1, 1]


def test_refresh_is_refused_within_cooldown(fake_refresh, monkeypatch):
    _, loaders = fake_refresh
    monkeypatch.setattr(app.time, "monotonic", lambda: 1000.0)
    app.request_refresh()

    monkeypatch.setattr(app.time, "monotonic", lambda: 1060.0)  # a minute later
    wait = app.request_refresh()

    assert wait == pytest.approx(config.REFRESH_COOLDOWN_SECONDS - 60)
    assert [loader.cleared for loader in loaders] == [1, 1]  # not cleared again

    later = 1000.0 + config.REFRESH_COOLDOWN_SECONDS
    monkeypatch.setattr(app.time, "monotonic", lambda: later)
    assert app.request_refresh() is None
    assert [loader.cleared for loader in loaders] == [2, 2]
