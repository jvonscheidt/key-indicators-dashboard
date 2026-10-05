"""Offline unit tests for the FRED fetcher.

``requests.get`` and the API-key lookup are monkeypatched, so these never
touch the network or the real key in ``.streamlit/secrets.toml``.
"""

from __future__ import annotations

import pandas as pd
import pytest
import requests

from config import HTTP_TIMEOUT_SECONDS
from data import fred

FAKE_KEY = "fake0123456789abcdef"


class _FakeResponse:
    def __init__(self, observations: list[tuple[str, str]]):
        self._payload = {
            "observations": [{"date": d, "value": v} for d, v in observations]
        }

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


@pytest.fixture(autouse=True)
def fake_key(monkeypatch):
    monkeypatch.setattr(fred, "get_fred_api_key", lambda: FAKE_KEY)


def test_fetch_fred_scales_and_skips_missing(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append((params, timeout))
        # FRED marks missing observations with ".".
        return _FakeResponse(
            [("2026-09-28", "1.40"), ("2026-09-29", "."), ("2026-09-30", "1.44")]
        )

    monkeypatch.setattr(fred.requests, "get", fake_get)
    res = fred.fetch_fred("EM spread", "BAMLEMCBPIOAS", 30, scale=100.0)

    assert res.ok
    assert res.value == pytest.approx(144.0)
    assert res.previous == pytest.approx(140.0)
    assert len(res.series) == 2
    assert res.timestamp == pd.Timestamp("2026-09-30").to_pydatetime()
    params, timeout = calls[0]
    assert timeout == HTTP_TIMEOUT_SECONDS
    assert params["series_id"] == "BAMLEMCBPIOAS"
    assert "observation_start" in params


def test_fetch_fred_trims_to_lookback(monkeypatch):
    dates = pd.date_range("2026-01-01", "2026-09-30", freq="D")
    observations = [(d.date().isoformat(), "80.0") for d in dates]
    monkeypatch.setattr(
        fred.requests, "get", lambda url, params, timeout: _FakeResponse(observations)
    )
    res = fred.fetch_fred("Brent", "DCOILBRENTEU", 30)

    assert res.ok
    # Window is anchored to the latest observation, inclusive of the cutoff.
    assert res.series.index.min() == pd.Timestamp("2026-08-31")
    assert len(res.series) == 31


def test_fetch_fred_without_key_degrades(monkeypatch):
    monkeypatch.setattr(fred, "get_fred_api_key", lambda: None)
    res = fred.fetch_fred("Brent", "DCOILBRENTEU", 30)
    assert not res.ok
    assert res.error == "FRED_API_KEY not set"


def test_client_error_is_not_retried_and_key_is_redacted(monkeypatch):
    calls = 0

    def fake_get(url, params, timeout):
        nonlocal calls
        calls += 1
        resp = requests.Response()
        resp.status_code = 400
        resp.reason = "Bad Request"
        resp.url = f"{url}?series_id=X&api_key={params['api_key']}&file_type=json"
        return resp

    monkeypatch.setattr(fred.requests, "get", fake_get)
    res = fred.fetch_fred("Brent", "DCOILBRENTEU", 30)

    assert not res.ok
    assert calls == 1  # a 4xx fails the same way every time
    assert FAKE_KEY not in res.error
    assert "api_key=***" in res.error


def test_redact_handles_url_encoded_key():
    message = "Invalid URL ...?api_key=ab%20cd&file_type=json"
    assert (
        fred._redact(message, "ab cd") == "Invalid URL ...?api_key=***&file_type=json"
    )
