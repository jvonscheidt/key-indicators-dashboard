"""Unit tests for the HTML scrapers (Risks §9: one test per scraper).

These run fully offline: the network-bound ``_get`` is monkeypatched to
return captured fixtures, so the tests exercise the parsing and the
graceful-degradation contract without hitting multpl.com or CBOE.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from data import scrape

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


@pytest.fixture
def cape_html() -> str:
    return _fixture("cape_sample.html")


@pytest.fixture
def putcall_html() -> str:
    return _fixture("putcall_sample.html")


# --------------------------------------------------------------------------
# Shiller CAPE
# --------------------------------------------------------------------------


def test_fetch_cape_parses_latest_and_history(monkeypatch, cape_html):
    monkeypatch.setattr(scrape, "_get", lambda url: cape_html)
    result = scrape.fetch_cape("Shiller CAPE", lookback_days=365 * 5)

    assert result.ok
    assert result.source == "scrape"
    assert result.value == pytest.approx(41.57)
    assert result.previous == pytest.approx(41.04)  # prior month
    assert len(result.series) == 5
    # Series is chronological so the last point is the latest value.
    assert result.series["value"].iloc[-1] == pytest.approx(41.57)
    assert result.timestamp.date().isoformat() == "2026-06-05"


def test_fetch_cape_short_lookback_keeps_two_points(monkeypatch, cape_html):
    # CAPE is monthly; a 5-day window spans no full month but must still
    # yield a delta rather than collapsing to a single point.
    monkeypatch.setattr(scrape, "_get", lambda url: cape_html)
    result = scrape.fetch_cape("Shiller CAPE", lookback_days=5)

    assert result.ok
    assert len(result.series) == 2
    assert result.delta_abs == pytest.approx(41.57 - 41.04)


def test_fetch_cape_failure_degrades(monkeypatch):
    monkeypatch.setattr(scrape, "_get", lambda url: "<html>no table here</html>")
    result = scrape.fetch_cape("Shiller CAPE", lookback_days=90)

    assert not result.ok
    assert result.value is None
    assert result.error  # message surfaced for the error badge (FR-06)


# --------------------------------------------------------------------------
# CBOE Put/Call
# --------------------------------------------------------------------------


def _render_day(template: str, date_str: str, value: str) -> str:
    """Re-stamp the captured page fixture with a given date + total value."""
    return template.replace("0.97", value).replace("2026-06-05", date_str)


def test_fetch_putcall_backfills_history(monkeypatch, putcall_html):
    # Simulate the ?dt= per-day pages: each queried date resolves to the most
    # recent trading day <= it (as the real site does), so weekends/holidays
    # collapse onto the prior session and de-duplicate.
    data = {"2026-06-03": "0.49", "2026-06-04": "0.44", "2026-06-05": "0.97"}
    sessions = sorted(data)

    def fake_get(url: str) -> str:
        dt = url.split("dt=")[-1]
        eligible = [d for d in sessions if d <= dt] or sessions[:1]
        day = eligible[-1]
        return _render_day(putcall_html, day, data[day])

    monkeypatch.setattr(scrape, "_get", fake_get)
    monkeypatch.setattr(
        scrape, "utcnow", lambda: datetime(2026, 6, 8, tzinfo=timezone.utc)
    )

    result = scrape.fetch_putcall("Put/Call Ratio", lookback_days=90)

    assert result.ok
    # fetched_at is when we retrieved it (mocked "now"), distinct from
    # timestamp (the latest session's trade date) checked below.
    assert result.fetched_at == datetime(2026, 6, 8, tzinfo=timezone.utc)
    assert len(result.series) == 3  # three distinct sessions, deduped
    assert list(result.series.index.strftime("%Y-%m-%d")) == sessions  # sorted
    assert result.value == pytest.approx(0.97)  # latest session
    assert result.previous == pytest.approx(0.44)  # prior session -> delta
    assert result.timestamp.date().isoformat() == "2026-06-05"


def test_fetch_putcall_single_session_no_delta(monkeypatch, putcall_html):
    # Every queried day returns the same session -> one point, no previous.
    monkeypatch.setattr(scrape, "_get", lambda url: putcall_html)
    result = scrape.fetch_putcall("Put/Call Ratio", lookback_days=90)

    assert result.ok
    assert len(result.series) == 1
    assert result.value == pytest.approx(0.97)  # total row, not EQUITY/INDEX
    assert result.previous is None
    assert result.timestamp.date().isoformat() == "2026-06-05"


def test_fetch_putcall_failure_degrades(monkeypatch):
    monkeypatch.setattr(scrape, "_get", lambda url: "<html>no ratios here</html>")
    result = scrape.fetch_putcall("Put/Call Ratio", lookback_days=90)

    assert not result.ok
    assert result.value is None
    assert result.error
