"""Market Indicators Live Dashboard — Streamlit UI (M3).

Single-page wide-mode dashboard (§8). Data fetching lives in the ``data/``
package and stays Streamlit-agnostic; this module adds the UI plus the
``st.cache_data`` TTL caching per source (NFR-02), the global lookback
selector (FR-03), auto/manual refresh (FR-04), threshold alerts (FR-05),
per-tile graceful degradation (FR-06), and freshness timestamps (FR-07).

Run with:  .venv/bin/streamlit run app.py
"""

from __future__ import annotations

import html
import time
from dataclasses import replace

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import (
    DEFAULT_LOOKBACK,
    FAILURE_RETRY_SECONDS,
    INDICATORS,
    LOOKBACK_OPTIONS,
    REFRESH_INTERVAL_SECONDS,
    SPARKLINE_DAYS,
    TTL_FRED,
    TTL_SCRAPE,
    TTL_YFINANCE,
)
from data.base import FetchResult
from data.fred import fetch_fred
from data.scrape import fetch_cape, fetch_putcall
from data.yf import fetch_price, fetch_sp500

PRIMARY = "#0078d4"
ACCENT = "#ff7f0e"
ALERT = "#d62728"

# --------------------------------------------------------------------------
# Cached loaders — one per source so each gets its own TTL (NFR-02). These
# wrap the pure fetchers; the FetchResult (incl. its DataFrame) is picklable
# so st.cache_data can memoize it keyed on the arguments. Failures are
# raised as _FetchFailed instead of returned: st.cache_data only memoizes
# successful returns, so a recovered source comes back on the next rerun
# rather than after the full source TTL.
# --------------------------------------------------------------------------


class _FetchFailed(Exception):
    """Carries a failed FetchResult out of a cached loader uncached."""

    def __init__(self, result: FetchResult):
        super().__init__(result.error)
        self.result = result


def _checked(result: FetchResult) -> FetchResult:
    if not result.ok:
        raise _FetchFailed(result)
    return result


@st.cache_data(ttl=TTL_YFINANCE, show_spinner=False)
def _load_price(label: str, symbol: str, lookback_days: int) -> FetchResult:
    return _checked(fetch_price(label, symbol, lookback_days))


@st.cache_data(ttl=TTL_YFINANCE, show_spinner=False)
def _load_sp500(label: str, symbol: str, lookback_days: int) -> FetchResult:
    return _checked(fetch_sp500(label, symbol, lookback_days))


@st.cache_data(ttl=TTL_FRED, show_spinner=False)
def _load_fred(
    label: str, symbol: str, lookback_days: int, scale: float
) -> FetchResult:
    return _checked(fetch_fred(label, symbol, lookback_days, scale))


@st.cache_data(ttl=TTL_SCRAPE, show_spinner=False)
def _load_cape(label: str, lookback_days: int) -> FetchResult:
    return _checked(fetch_cape(label, lookback_days))


@st.cache_data(ttl=TTL_SCRAPE, show_spinner=False)
def _load_putcall(label: str, lookback_days: int) -> FetchResult:
    return _checked(fetch_putcall(label, lookback_days))


def _fetch(key: str, lookback_days: int) -> FetchResult:
    """Dispatch one indicator to its cached loader by source."""
    ind = INDICATORS[key]
    try:
        if ind.source == "yfinance":
            if key == "sp500":
                return _load_sp500(ind.label, ind.symbol, lookback_days)
            return _load_price(ind.label, ind.symbol, lookback_days)
        if ind.source == "fred":
            return _load_fred(ind.label, ind.symbol, lookback_days, ind.scale)
        if ind.source == "scrape":
            if key == "cape":
                return _load_cape(ind.label, lookback_days)
            return _load_putcall(ind.label, lookback_days)
    except _FetchFailed as exc:
        return exc.result
    return FetchResult.failure(ind.source, ind.label, f"unknown source {ind.source}")


def with_last_good(
    result: FetchResult,
    store: dict[tuple[str, int], FetchResult],
    key: tuple[str, int],
) -> FetchResult:
    """Fallback to the last good result when a fresh fetch fails (NFR-03).

    Successes are recorded in ``store``; on failure the stored result is
    served marked ``stale`` (carrying the fresh error) so the tile can show
    a staleness badge (Risks §9) instead of dropping to an error badge. A
    failure with no prior success passes through unchanged (FR-06).
    """
    if result.ok:
        store[key] = result
        return result
    last = store.get(key)
    if last is None:
        return result
    return replace(last, stale=True, error=result.error)


def load(key: str, lookback_days: int) -> FetchResult:
    """Fetch one indicator, serving the session's last good value on failure.

    Failures bypass st.cache_data (see the loaders above), so a recovered
    source is retried on the next rerun. A session-level memo throttles those
    retries to one per ``FAILURE_RETRY_SECONDS`` while the source is still
    down, keeping widget interactions responsive during an outage.
    """
    store = st.session_state.setdefault("_last_good", {})
    failures = st.session_state.setdefault("_recent_failures", {})
    memo_key = (key, lookback_days)
    memo = failures.get(memo_key)
    if memo is not None and time.monotonic() - memo[0] < FAILURE_RETRY_SECONDS:
        result = memo[1]
    else:
        result = _fetch(key, lookback_days)
        if result.ok:
            failures.pop(memo_key, None)
        else:
            failures[memo_key] = (time.monotonic(), result)
    return with_last_good(result, store, memo_key)


# --------------------------------------------------------------------------
# Formatting & threshold helpers
# --------------------------------------------------------------------------


def fmt_value(key: str, value: float | None) -> str:
    if value is None:
        return "—"
    ind = INDICATORS[key]
    if key == "eurusd":
        return f"{value:.4f}"
    if ind.unit == "bps":
        return f"{value:,.0f} bps"
    return f"{value:,.2f}"


def fmt_delta(key: str, result: FetchResult) -> str | None:
    if result.delta_abs is None:
        return None
    decimals = 4 if key == "eurusd" else 2
    out = f"{result.delta_abs:+,.{decimals}f}"
    if result.delta_pct is not None:
        out += f" ({result.delta_pct:+.2f}%)"
    return out


def effective_level(key: str) -> float | None:
    """Threshold level for ``key``, honouring any session-state override."""
    ind = INDICATORS[key]
    if ind.threshold is None:
        return None
    return float(st.session_state.get(f"thr_{key}", ind.threshold.level))


def is_breached(key: str, value: float | None) -> bool:
    ind = INDICATORS[key]
    if ind.threshold is None or value is None:
        return False
    level = effective_level(key)
    return value > level if ind.threshold.direction == "above" else value < level


# --------------------------------------------------------------------------
# Charts (Plotly graph objects)
# --------------------------------------------------------------------------


def _bare_layout(fig: go.Figure, height: int) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=0, r=0, t=4, b=0),
        showlegend=False,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    return fig


def sparkline(series: pd.DataFrame) -> go.Figure:
    """Tiny last-90-day line for a metric tile (FR-02)."""
    col = series.columns[0]
    s = series[col].dropna()
    if not s.empty:
        cutoff = s.index.max() - pd.Timedelta(days=SPARKLINE_DAYS)
        s = s[s.index >= cutoff]
    fig = go.Figure(
        go.Scatter(x=s.index, y=s, mode="lines", line=dict(color=PRIMARY, width=2))
    )
    fig = _bare_layout(fig, height=80)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


def sp500_chart(series: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=series.index, y=series["price"], name="S&P 500", line=dict(color=PRIMARY)
        )
    )
    fig.add_trace(
        go.Scatter(
            x=series.index,
            y=series["ma200"],
            name="200-day MA",
            line=dict(color=ACCENT, dash="dash"),
        )
    )
    fig.update_layout(
        height=320,
        margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(orientation="h", y=1.05),
    )
    return fig


def line_chart(series: pd.DataFrame, color: str = PRIMARY) -> go.Figure:
    col = series.columns[0]
    fig = go.Figure(
        go.Scatter(x=series.index, y=series[col], mode="lines", line=dict(color=color))
    )
    fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0))
    return fig


def putcall_chart(series: pd.DataFrame, level: float | None) -> go.Figure:
    col = series.columns[0]
    s = series[col]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=series.index, y=s, name="Put/Call", marker_color=PRIMARY))
    if len(s) >= 2:
        fig.add_trace(
            go.Scatter(
                x=series.index,
                y=s.rolling(10, min_periods=1).mean(),
                name="10-day avg",
                line=dict(color=ACCENT),
            )
        )
    if level is not None:
        fig.add_hline(y=level, line=dict(color=ALERT, dash="dot"))
    fig.update_layout(
        height=320,
        margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(orientation="h", y=1.05),
    )
    return fig


def area_chart(series: pd.DataFrame, level: float | None) -> go.Figure:
    col = series.columns[0]
    fig = go.Figure(
        go.Scatter(
            x=series.index,
            y=series[col],
            mode="lines",
            fill="tozeroy",
            line=dict(color=PRIMARY),
        )
    )
    if level is not None:
        fig.add_hline(
            y=level,
            line=dict(color=ALERT, dash="dot"),
            annotation_text=f"alert {level:g}",
        )
    fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0))
    return fig


# --------------------------------------------------------------------------
# Tile / panel renderers
# --------------------------------------------------------------------------


def stale_badge(result: FetchResult) -> None:
    """Amber badge when serving a last-good value after a failed refresh."""
    if not result.stale:
        return
    tooltip = html.escape(result.error or "", quote=True)
    st.markdown(
        f"<span title='{tooltip}' style='color:{ACCENT};font-weight:600'>"
        "🕓 STALE — refresh failed, showing last good data</span>",
        unsafe_allow_html=True,
    )


def metric_tile(container, key: str, result: FetchResult) -> None:
    """Top-row metric tile: value, delta, alert badge, sparkline (FR-02/05/06)."""
    ind = INDICATORS[key]
    with container:
        if not result.ok:
            st.metric(ind.label, "—")
            st.error(f"⚠️ {result.error}", icon="🚫")
            return
        # Risk indicators (threshold "above") read better with inverse colors:
        # a rise is bad, so show it red.
        inverse = ind.threshold is not None and ind.threshold.direction == "above"
        st.metric(
            ind.label,
            fmt_value(key, result.value),
            fmt_delta(key, result),
            delta_color="inverse" if inverse else "normal",
        )
        if is_breached(key, result.value):
            st.markdown(
                f"<span style='color:{ALERT};font-weight:600'>⚠ ALERT — "
                f"{ind.threshold.direction} {effective_level(key):g}</span>",
                unsafe_allow_html=True,
            )
        stale_badge(result)
        st.plotly_chart(
            sparkline(result.series), width="stretch", config={"displayModeBar": False}
        )


def panel(container, key: str, result: FetchResult, figure_fn) -> None:
    """Second/third-row chart panel with header, current value, and alert."""
    ind = INDICATORS[key]
    with container:
        st.subheader(ind.label)
        if not result.ok:
            st.error(f"⚠️ {result.error}", icon="🚫")
            return
        cols = st.columns([1, 1])
        cols[0].metric("Current", fmt_value(key, result.value), fmt_delta(key, result))
        if is_breached(key, result.value):
            cols[1].markdown(
                f"<div style='padding-top:18px;color:{ALERT};font-weight:600'>⚠ ALERT — "
                f"{ind.threshold.direction} {effective_level(key):g}</div>",
                unsafe_allow_html=True,
            )
        stale_badge(result)
        st.plotly_chart(figure_fn(result.series), width="stretch")


# --------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------


def render_sidebar() -> tuple[int, bool]:
    """Draw sidebar controls; return (lookback_days, auto_refresh)."""
    with st.sidebar:
        st.header("⚙️ Controls")
        label = st.selectbox(
            "Lookback period",
            list(LOOKBACK_OPTIONS),
            index=list(LOOKBACK_OPTIONS).index(DEFAULT_LOOKBACK),
        )
        auto = st.toggle(
            "Auto-refresh",
            value=False,
            help=f"Re-render every {REFRESH_INTERVAL_SECONDS // 60} min",
        )
        if st.button("🔄 Refresh now", width="stretch"):
            st.cache_data.clear()
            # Also drop the failure-retry memo so a manual refresh always
            # re-attempts sources that recently failed.
            st.session_state.pop("_recent_failures", None)
            st.rerun()

        with st.expander("Alert thresholds"):
            for key, ind in INDICATORS.items():
                if ind.threshold is None:
                    continue
                st.number_input(
                    f"{ind.label} ({ind.threshold.direction})",
                    value=float(ind.threshold.level),
                    step=1.0,
                    key=f"thr_{key}",
                )
    return LOOKBACK_OPTIONS[label], auto


def render_freshness(slot, results: dict[str, FetchResult]) -> None:
    """Per-source data freshness timestamps (FR-07).

    ``slot`` is an ``st.empty`` placeholder created outside the auto-refresh
    fragment. Writing into an outside container from a fragment rerun is
    additive (elements accumulate until the next full run), but writing into
    ``st.empty`` *replaces* its content — so each refresh redraws the captions
    instead of duplicating them.
    """
    latest: dict[str, pd.Timestamp | None] = {}
    for res in results.values():
        if res.fetched_at is None:
            continue
        ts = pd.Timestamp(res.fetched_at)
        if res.source not in latest or (
            latest[res.source] is not None and ts > latest[res.source]
        ):
            latest[res.source] = ts
    with slot.container():
        st.caption("**Data freshness** (last successful fetch)")
        for source in ("yfinance", "fred", "scrape"):
            ts = latest.get(source)
            shown = ts.strftime("%Y-%m-%d %H:%M:%S UTC") if ts is not None else "—"
            st.caption(f"{source}: {shown}")


# --------------------------------------------------------------------------
# Main layout
# --------------------------------------------------------------------------


def render_dashboard(lookback_days: int, freshness_slot) -> None:
    """Fetch all eight indicators and lay out the page (§8)."""
    results = {key: load(key, lookback_days) for key in INDICATORS}

    # Top row — four metric tiles.
    top = st.columns(4)
    for col, key in zip(top, ("vix", "dxy", "eurusd", "brent")):
        metric_tile(col, key, results[key])

    st.divider()

    # Second row — S&P 500 vs MA | Shiller CAPE.
    r2 = st.columns(2)
    panel(r2[0], "sp500", results["sp500"], sp500_chart)
    panel(r2[1], "cape", results["cape"], lambda s: line_chart(s, ACCENT))

    st.divider()

    # Third row — Put/Call | EM spread.
    r3 = st.columns(2)
    panel(
        r3[0],
        "putcall",
        results["putcall"],
        lambda s: putcall_chart(s, effective_level("putcall")),
    )
    panel(
        r3[1],
        "em_spread",
        results["em_spread"],
        lambda s: area_chart(s, effective_level("em_spread")),
    )

    render_freshness(freshness_slot, results)


def main() -> None:
    st.set_page_config(page_title="Market Indicators", page_icon="📈", layout="wide")
    st.title("📈 Market Indicators Live Dashboard")

    lookback_days, auto = render_sidebar()
    # An st.empty placeholder (not a plain container): render_freshness runs
    # inside the fragment below, and only st.empty replaces its previous
    # content on fragment reruns — a container would accumulate duplicates.
    freshness_slot = st.sidebar.empty()

    # Auto-refresh (FR-04): wrap the body in a fragment that re-runs on the
    # configured interval when enabled; cached loaders keep it cheap (NFR-02).
    interval = REFRESH_INTERVAL_SECONDS if auto else None
    st.fragment(render_dashboard, run_every=interval)(lookback_days, freshness_slot)


if __name__ == "__main__":
    main()
