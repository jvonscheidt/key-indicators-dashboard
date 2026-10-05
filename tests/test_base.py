"""Unit tests for the shared retry policy (NFR-03)."""

from __future__ import annotations

import pytest
import requests

from data.base import is_transient


def _http_error(status: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(response=resp)


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_errors_are_not_retried(status):
    assert not is_transient(_http_error(status))


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
def test_timeouts_rate_limits_and_server_errors_are_retried(status):
    assert is_transient(_http_error(status))


def test_network_and_other_errors_are_retried():
    assert is_transient(requests.ConnectionError("reset"))
    assert is_transient(requests.Timeout("slow"))
    assert is_transient(ValueError("yfinance returned no data"))
