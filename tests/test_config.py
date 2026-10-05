"""Unit tests for config's FRED key lookup.

``streamlit`` is stubbed in ``sys.modules`` so the lookup never reads the
real ``.streamlit/secrets.toml``.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import config


def _stub_secrets(monkeypatch, secrets: dict) -> None:
    monkeypatch.setitem(sys.modules, "streamlit", SimpleNamespace(secrets=secrets))


def test_secret_is_stripped(monkeypatch):
    _stub_secrets(monkeypatch, {"FRED_API_KEY": "  abc123 \n"})
    assert config.get_fred_api_key() == "abc123"


def test_env_fallback_is_stripped(monkeypatch):
    _stub_secrets(monkeypatch, {})
    monkeypatch.setenv("FRED_API_KEY", " xyz789 ")
    assert config.get_fred_api_key() == "xyz789"


def test_blank_key_counts_as_unset(monkeypatch):
    _stub_secrets(monkeypatch, {})
    monkeypatch.setenv("FRED_API_KEY", "   ")
    assert config.get_fred_api_key() is None


def test_missing_key_is_none(monkeypatch):
    _stub_secrets(monkeypatch, {})
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    assert config.get_fred_api_key() is None
