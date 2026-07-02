"""Shared primitives for the data layer.

Every fetcher returns a :class:`FetchResult` so the UI can treat all seven
sources uniformly: render on success, show an error badge on failure
(FR-06), and surface a freshness/staleness signal (FR-07, Risks §9).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from config import RETRY_ATTEMPTS, RETRY_BACKOFF_BASE


@dataclass
class FetchResult:
    """Uniform return contract for all data fetchers.

    Attributes:
        source: One of "yfinance" | "fred" | "scrape".
        label: Human-readable indicator name.
        value: Latest scalar value, or ``None`` on failure.
        previous: Prior period value, for delta computation.
        series: Time-indexed DataFrame for charting (may be empty).
        timestamp: The as-of date of the latest data point itself (a trading
            day / series date / trade date). These sources are daily-or-
            coarser, so this carries no meaningful time-of-day.
        fetched_at: Wall-clock UTC time this fetcher last actually retrieved
            the data from its source (FR-07). Distinct from ``timestamp``:
            this is when *we* fetched, not what date the data represents.
        ok: ``True`` if the fetch succeeded.
        error: Error message when ``ok`` is ``False``.
        stale: ``True`` when serving a cached/last-good value after a failure.
    """

    source: str
    label: str
    value: float | None = None
    previous: float | None = None
    series: pd.DataFrame = field(default_factory=pd.DataFrame)
    timestamp: datetime | None = None
    fetched_at: datetime | None = None
    ok: bool = True
    error: str | None = None
    stale: bool = False

    @property
    def delta_abs(self) -> float | None:
        if self.value is None or self.previous is None:
            return None
        return self.value - self.previous

    @property
    def delta_pct(self) -> float | None:
        d = self.delta_abs
        if d is None or not self.previous:
            return None
        return d / self.previous * 100.0

    @classmethod
    def failure(cls, source: str, label: str, error: str) -> "FetchResult":
        return cls(source=source, label=label, ok=False, error=error)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def with_retry(func):
    """Decorator: retry an HTTP-bound fetch with exponential backoff (NFR-03).

    Wraps :mod:`tenacity` so the policy stays centralized in ``config.py``.
    Retries on any exception; the caller is responsible for turning a final
    failure into a :class:`FetchResult.failure`.
    """

    return retry(
        reraise=True,
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=RETRY_BACKOFF_BASE),
    )(func)
