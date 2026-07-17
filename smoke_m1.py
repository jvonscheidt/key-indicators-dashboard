"""M1 smoke test: exercise every yfinance + FRED fetcher and print results.

Throwaway verification script. Run with:  python smoke_m1.py
"""

from __future__ import annotations

from config import INDICATORS, LOOKBACK_OPTIONS
from data.fred import fetch_fred
from data.yf import fetch_price, fetch_sp500

LOOKBACK_DAYS = LOOKBACK_OPTIONS["3M"]


def _show(result) -> None:
    status = "OK " if result.ok else "ERR"
    val = f"{result.value:.4f}" if result.value is not None else "—"
    ts = result.timestamp.date().isoformat() if result.timestamp else "—"
    rows = len(result.series)
    cols = list(result.series.columns)
    line = f"[{status}] {result.label:<28} value={val:<12} asof={ts}  rows={rows} cols={cols}"
    if not result.ok:
        line += f"  error={result.error}"
    print(line)


def main() -> None:
    print(f"Lookback: 3M ({LOOKBACK_DAYS}d)\n")

    for key in ("vix", "dxy", "eurusd"):
        ind = INDICATORS[key]
        _show(fetch_price(ind.label, ind.symbol, LOOKBACK_DAYS))

    sp = INDICATORS["sp500"]
    _show(fetch_sp500(sp.label, sp.symbol, LOOKBACK_DAYS))

    for key in ("em_spread", "brent"):
        ind = INDICATORS[key]
        _show(fetch_fred(ind.label, ind.symbol, LOOKBACK_DAYS, ind.scale))


if __name__ == "__main__":
    main()
