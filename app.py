"""Market Indicators Live Dashboard — Streamlit UI (M3).

Single-page wide-mode dashboard (§8). Data fetching lives in the ``data/``
package and stays Streamlit-agnostic; this module adds the UI plus the
``st.cache_data`` TTL caching per source (NFR-02), the global lookback
selector (FR-03), auto/manual refresh (FR-04), threshold alerts (FR-05),
per-tile graceful degradation (FR-06), and freshness timestamps (FR-07).

Run with:  streamlit run app.py
"""

from __future__ import annotations

import html
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

from config import (
    DEFAULT_LOOKBACK,
    FAILURE_RETRY_SECONDS,
    INDICATORS,
    LOOKBACK_OPTIONS,
    MAX_LOOKBACK_DAYS,
    REFRESH_COOLDOWN_SECONDS,
    REFRESH_INTERVAL_SECONDS,
    SPARKLINE_DAYS,
    TTL_FRED,
    TTL_SCRAPE,
    TTL_YFINANCE,
)
from data.base import FetchResult, utcnow
from data.fred import fetch_fred
from data.scrape import fetch_cape, fetch_putcall
from data.yf import fetch_price, fetch_sp500

PRIMARY = "#0078d4"
ACCENT = "#ff7f0e"
ALERT = "#d62728"

# --------------------------------------------------------------------------
# Cached loaders — one per source so each gets its own TTL (NFR-02). These
# wrap the pure fetchers; the FetchResult (incl. its DataFrame) is picklable
# so st.cache_data can memoize it keyed on the arguments. Every source is
# fetched once at MAX_LOOKBACK_DAYS and trimmed per lookback in the UI
# (for_lookback), so the lookback is not part of any cache key and changing
# it never refetches. Failures are raised as _FetchFailed instead of
# returned: st.cache_data only memoizes successful returns, so a recovered
# source comes back on the next rerun rather than after the full source TTL.
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
def _load_price(label: str, symbol: str) -> FetchResult:
    return _checked(fetch_price(label, symbol, MAX_LOOKBACK_DAYS))


@st.cache_data(ttl=TTL_YFINANCE, show_spinner=False)
def _load_sp500(label: str, symbol: str) -> FetchResult:
    return _checked(fetch_sp500(label, symbol, MAX_LOOKBACK_DAYS))


@st.cache_data(ttl=TTL_FRED, show_spinner=False)
def _load_fred(label: str, symbol: str, scale: float) -> FetchResult:
    return _checked(fetch_fred(label, symbol, MAX_LOOKBACK_DAYS, scale))


@st.cache_data(ttl=TTL_SCRAPE, show_spinner=False)
def _load_cape(label: str) -> FetchResult:
    return _checked(fetch_cape(label, MAX_LOOKBACK_DAYS))


@st.cache_data(ttl=TTL_SCRAPE, show_spinner=False)
def _load_putcall(label: str) -> FetchResult:
    return _checked(fetch_putcall(label, MAX_LOOKBACK_DAYS))


_LOADERS = (_load_price, _load_sp500, _load_fred, _load_cape, _load_putcall)


def _fetch(key: str) -> FetchResult:
    """Dispatch one indicator to its cached loader by source."""
    ind = INDICATORS[key]
    try:
        if ind.source == "yfinance":
            if key == "sp500":
                return _load_sp500(ind.label, ind.symbol)
            return _load_price(ind.label, ind.symbol)
        if ind.source == "fred":
            return _load_fred(ind.label, ind.symbol, ind.scale)
        if ind.source == "scrape":
            if key == "cape":
                return _load_cape(ind.label)
            return _load_putcall(ind.label)
    except _FetchFailed as exc:
        return exc.result
    return FetchResult.failure(ind.source, ind.label, f"unknown source {ind.source}")


@st.cache_resource
def _refresh_clock() -> dict:
    """When "Refresh now" last cleared the caches, shared by all sessions."""
    return {"lock": threading.Lock(), "at": None}


def request_refresh() -> float | None:
    """Clear the data caches unless a refresh ran within the cooldown.

    The caches are shared by every visitor of the public app, so a refresh
    re-hits every source for everyone. Returns ``None`` if the caches were
    cleared, otherwise the seconds left until a refresh is allowed again.
    """
    clock = _refresh_clock()
    with clock["lock"]:
        now = time.monotonic()
        if clock["at"] is not None and now - clock["at"] < REFRESH_COOLDOWN_SECONDS:
            return REFRESH_COOLDOWN_SECONDS - (now - clock["at"])
        clock["at"] = now
    for loader in _LOADERS:
        loader.clear()
    return None


def with_last_good(
    result: FetchResult, store: dict[str, FetchResult], key: str
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


def load_all() -> dict[str, FetchResult]:
    """Fetch every indicator, serving the session's last good value on failure.

    Results cover ``MAX_LOOKBACK_DAYS``; trim them with :func:`for_lookback`.

    Sources are fetched concurrently, so a slow or hanging source delays the
    page by its own latency rather than adding to every other source's (FR-06,
    NFR-01). Only the cached ``_fetch`` calls run on worker threads; session
    state is read and written on the script thread.

    Failures bypass st.cache_data (see the loaders above), so a recovered
    source is retried on the next rerun. A session-level memo throttles those
    retries to one per ``FAILURE_RETRY_SECONDS`` while the source is still
    down, keeping widget interactions responsive during an outage.
    """
    store = st.session_state.setdefault("_last_good", {})
    failures = st.session_state.setdefault("_recent_failures", {})
    results: dict[str, FetchResult] = {}
    pending = []
    for key in INDICATORS:
        memo = failures.get(key)
        if memo is not None and time.monotonic() - memo[0] < FAILURE_RETRY_SECONDS:
            results[key] = memo[1]
        else:
            pending.append(key)

    ctx = get_script_run_ctx()

    def fetch(key: str) -> FetchResult:
        add_script_run_ctx(threading.current_thread(), ctx)
        return _fetch(key)

    if pending:
        with ThreadPoolExecutor(max_workers=len(pending)) as pool:
            for key, result in zip(pending, pool.map(fetch, pending), strict=True):
                if result.ok:
                    failures.pop(key, None)
                else:
                    failures[key] = (time.monotonic(), result)
                results[key] = result
    return {key: with_last_good(results[key], store, key) for key in INDICATORS}


# --------------------------------------------------------------------------
# Lookback trimming & data age (FR-03, Risks §9)
# --------------------------------------------------------------------------

#: Minimum slack before a series counts as starting after the lookback window,
#: so weekends and holidays at the window edge don't trigger the history note.
#: Coarser series get more (see history_start).
_HISTORY_SLACK_DAYS = 7


def for_lookback(result: FetchResult, lookback_days: int) -> FetchResult:
    """Trim a full-window result's series to the selected lookback (FR-03).

    The window is anchored to the latest observation. At least two points
    are kept so a coarse series (CAPE is monthly) still draws a line; the
    series is never interpolated (Risks §9).
    """
    series = result.series
    if not result.ok or series.empty:
        return result
    cutoff = series.index.max() - pd.Timedelta(days=lookback_days)
    trimmed = series[series.index >= cutoff]
    if len(trimmed) < 2:
        trimmed = series.tail(2)
    return replace(result, series=trimmed)


def history_start(result: FetchResult, lookback_days: int) -> pd.Timestamp | None:
    """First date of the series if the source has no data back to the window.

    CBOE Put/Call is only backfilled a few weeks, and FRED only carries about
    three years of the ICE BofA EM spread; this lets the panel say so instead
    of silently showing a shorter window.
    """
    series = result.series
    if not result.ok or series.empty:
        return None
    start = series.index.min()
    cutoff = series.index.max() - pd.Timedelta(days=lookback_days)
    # A monthly series (CAPE) can start up to a month after the cutoff with no
    # history missing, so allow two typical gaps between points.
    gap = series.index.to_series().diff().median()
    slack = pd.Timedelta(days=_HISTORY_SLACK_DAYS)
    if pd.notna(gap):
        slack = max(slack, 2 * gap)
    if start - cutoff > slack:
        return start
    return None


def data_age_days(result: FetchResult, today: date) -> int | None:
    """Calendar days between ``today`` and the latest data point's date."""
    if result.timestamp is None:
        return None
    return (today - result.timestamp.date()).days


def is_outdated(key: str, result: FetchResult, today: date) -> bool:
    """Whether the latest data is older than the indicator normally runs."""
    age = data_age_days(result, today)
    return age is not None and age > INDICATORS[key].max_age_days


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
        margin={"l": 0, "r": 0, "t": 4, "b": 0},
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
        go.Scatter(x=s.index, y=s, mode="lines", line={"color": PRIMARY, "width": 2})
    )
    fig = _bare_layout(fig, height=80)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


def sp500_chart(series: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=series.index, y=series["price"], name="S&P 500", line={"color": PRIMARY}
        )
    )
    fig.add_trace(
        go.Scatter(
            x=series.index,
            y=series["ma200"],
            name="200-day MA",
            line={"color": ACCENT, "dash": "dash"},
        )
    )
    fig.update_layout(
        height=320,
        margin={"l": 0, "r": 0, "t": 10, "b": 0},
        legend={"orientation": "h", "y": 1.05},
    )
    return fig


def line_chart(series: pd.DataFrame, color: str = PRIMARY) -> go.Figure:
    col = series.columns[0]
    fig = go.Figure(
        go.Scatter(x=series.index, y=series[col], mode="lines", line={"color": color})
    )
    fig.update_layout(height=320, margin={"l": 0, "r": 0, "t": 10, "b": 0})
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
                line={"color": ACCENT},
            )
        )
    if level is not None:
        fig.add_hline(y=level, line={"color": ALERT, "dash": "dot"})
    fig.update_layout(
        height=320,
        margin={"l": 0, "r": 0, "t": 10, "b": 0},
        legend={"orientation": "h", "y": 1.05},
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
            line={"color": PRIMARY},
        )
    )
    if level is not None:
        fig.add_hline(
            y=level,
            line={"color": ALERT, "dash": "dot"},
            annotation_text=f"alert {level:g}",
        )
    fig.update_layout(height=320, margin={"l": 0, "r": 0, "t": 10, "b": 0})
    return fig


# --------------------------------------------------------------------------
# Tile / panel renderers
# --------------------------------------------------------------------------


def freshness_badges(key: str, result: FetchResult) -> None:
    """As-of date of the data, plus amber badges when it is stale (Risks §9).

    Two independent cases: the latest data point is older than the indicator
    normally runs (the source stopped publishing, or monthly CAPE is late),
    and a refresh failed so the session's last good data is being served.
    """
    today = utcnow().date()
    if result.timestamp is not None:
        as_of = f"{result.timestamp:%Y-%m-%d}"
        if is_outdated(key, result, today):
            age = data_age_days(result, today)
            st.markdown(
                f"<span style='color:{ACCENT};font-weight:600'>"
                f"🕓 OUTDATED — latest data is {age} days old (as of {as_of})</span>",
                unsafe_allow_html=True,
            )
        else:
            st.caption(f"As of {as_of}")
    if result.stale:
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
        freshness_badges(key, result)
        # The full cached window, not the lookback: the sparkline always
        # covers SPARKLINE_DAYS, even on the 1M lookback (FR-02).
        st.plotly_chart(
            sparkline(result.series), width="stretch", config={"displayModeBar": False}
        )


def panel(
    container, key: str, result: FetchResult, figure_fn, lookback_days: int
) -> None:
    """Second/third-row chart panel with header, current value, and alert."""
    ind = INDICATORS[key]
    with container:
        st.subheader(ind.label)
        if not result.ok:
            st.error(f"⚠️ {result.error}", icon="🚫")
            return
        start = history_start(result, lookback_days)
        result = for_lookback(result, lookback_days)
        cols = st.columns([1, 1])
        cols[0].metric("Current", fmt_value(key, result.value), fmt_delta(key, result))
        if is_breached(key, result.value):
            cols[1].markdown(
                f"<div style='padding-top:18px;color:{ALERT};font-weight:600'>⚠ ALERT — "
                f"{ind.threshold.direction} {effective_level(key):g}</div>",
                unsafe_allow_html=True,
            )
        freshness_badges(key, result)
        if start is not None:
            st.caption(
                f"History starts {start:%Y-%m-%d}: the source has no earlier "
                "data, so the chart is shorter than the selected lookback."
            )
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
            wait = request_refresh()
            if wait is None:
                # Also drop the failure-retry memo so a manual refresh always
                # re-attempts sources that recently failed.
                st.session_state.pop("_recent_failures", None)
                st.rerun()
            st.toast(
                f"Data was refreshed in the last {REFRESH_COOLDOWN_SECONDS // 60} "
                f"min. Try again in {math.ceil(wait / 60)} min."
            )

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

    Shows the *oldest* fetch per source, so one indicator serving stale data
    isn't hidden behind a sibling from the same source that refreshed fine.
    """
    oldest: dict[str, pd.Timestamp] = {}
    for res in results.values():
        if res.fetched_at is None:
            continue
        ts = pd.Timestamp(res.fetched_at)
        if res.source not in oldest or ts < oldest[res.source]:
            oldest[res.source] = ts
    with slot.container():
        st.caption("**Data freshness** (oldest successful fetch per source)")
        for source in ("yfinance", "fred", "scrape"):
            ts = oldest.get(source)
            shown = ts.strftime("%Y-%m-%d %H:%M:%S UTC") if ts is not None else "—"
            st.caption(f"{source}: {shown}")


# --------------------------------------------------------------------------
# Main layout
# --------------------------------------------------------------------------


def render_dashboard(lookback_days: int, freshness_slot) -> None:
    """Fetch all eight indicators and lay out the page (§8)."""
    results = load_all()

    # Top row — four metric tiles (value + fixed-length sparkline; the
    # lookback only applies to the panel charts below).
    top = st.columns(4)
    for col, key in zip(top, ("vix", "dxy", "eurusd", "brent"), strict=True):
        metric_tile(col, key, results[key])

    st.divider()

    # Second row — S&P 500 vs MA | Shiller CAPE.
    r2 = st.columns(2)
    panel(r2[0], "sp500", results["sp500"], sp500_chart, lookback_days)
    panel(
        r2[1], "cape", results["cape"], lambda s: line_chart(s, ACCENT), lookback_days
    )

    st.divider()

    # Third row — Put/Call | EM spread.
    r3 = st.columns(2)
    panel(
        r3[0],
        "putcall",
        results["putcall"],
        lambda s: putcall_chart(s, effective_level("putcall")),
        lookback_days,
    )
    panel(
        r3[1],
        "em_spread",
        results["em_spread"],
        lambda s: area_chart(s, effective_level("em_spread")),
        lookback_days,
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
