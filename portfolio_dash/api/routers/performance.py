"""GET /api/performance/twr — portfolio time-weighted return vs a benchmark (FU-D27).

Pure read + analysis serialization. The portfolio's daily NAV series is the SAME
``daily_value_series`` the dashboard trend card plots (obtained via ``build_dashboard``),
so the TWR is always consistent with the 市值 chart. Benchmark closes come from the
``prices`` table (populated by the history job / smart backfill; a benchmark is NOT a
registered instrument), converted to the reporting currency at daily carry-forward FX so
the comparison embeds FX exactly like the portfolio does. Both series are rebased to 100 at
their common start.

This is an ANALYSIS metric, not money-of-record: every number is a Decimal STRING, and a
missing benchmark / price / FX degrades to ``available=false`` + a zh reason — it never
500s. Session-gating is the app-global ``require_session`` (open in guest mode), same as
every other read endpoint.
"""

import sqlite3
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.data_ingestion.holdings import load_action_index
from portfolio_dash.portfolio.dashboard import build_dashboard, closes_in
from portfolio_dash.portfolio.timeseries import FxHistory
from portfolio_dash.portfolio.twr import build_overlay, convert_closes, twr_index
from portfolio_dash.pricing.benchmarks import get_benchmark
from portfolio_dash.pricing.store import get_close_series, get_fx_series
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.wire import decimal_str

router = APIRouter()

_WINDOW_DAYS = {"1y": 365, "3y": 1095}
_WINDOWS = {"1y", "3y", "all"}
_WIRE_DP = Decimal("0.0001")  # 4 dp at the wire (index values are analysis, not money)

_BASIS_NOTES = {
    "portfolio": "時間加權報酬（報告幣）",
    "benchmark": "指數價格報酬（不含股息，換算報告幣）",
}


def _wire4(value: Decimal) -> str:
    """Quantize an index value to 4 dp (ROUND_HALF_UP) and render the canonical string."""
    return decimal_str(value.quantize(_WIRE_DP, rounding=ROUND_HALF_UP))


def _load_fx_history(
    conn: sqlite3.Connection, quote: Currency, reporting: Currency, start: date, as_of: date
) -> FxHistory:
    """Both directions of the ``quote``/``reporting`` pair for carry-forward conversion of
    closes dated from *start* — each read from its last rate on or before *start*."""
    history: FxHistory = {}
    if quote == reporting:
        return history
    for base, other in ((quote, reporting), (reporting, quote)):
        rows = get_fx_series(conn, base, other, start, as_of)
        if rows:
            history[(base, other)] = rows
    return history


@router.get("/performance/twr")
def performance_twr(
    benchmark: str = Query("0050"),
    window: str = Query("1y"),
    conn: sqlite3.Connection = Depends(get_conn, scope="function"),
    now: datetime = Depends(get_now),
    reporting: Currency = Depends(get_reporting),
) -> dict[str, Any]:
    """Portfolio TWR vs a benchmark, rebased to 100, as Decimal-string series."""
    bench = get_benchmark(benchmark)
    if bench is None:
        raise HTTPException(status_code=404, detail=f"未知的比較基準：{benchmark}")
    if window not in _WINDOWS:
        raise HTTPException(status_code=400, detail=f"未知的時間範圍：{window}")

    as_of = now.date()
    base: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "window": window,
        "benchmark": {"key": bench.key, "label": bench.label},
        "basis_notes": _BASIS_NOTES,
    }

    # Portfolio TWR from the same daily NAV series the dashboard trend card uses.
    data = build_dashboard(conn, now=now, reporting=reporting)
    # Options spec §5 / R3: that series leaves out an open contract's value, so the TWR built
    # on it does too — said with the dashboard's own note, on every answer (available or not).
    # ADDITIVE and only then: a ledger that never skipped a contract keeps its payload exactly.
    if data.options is not None and data.options.trend_excludes_value:
        base["options_note"] = data.options.note
    port_index = twr_index(data.trend.points) if data.trend.available else []
    if not port_index:
        return {
            **base, "available": False, "points": [],
            "reason": "投資組合每日淨值資料不足，無法計算時間加權報酬",
        }

    window_start = (
        port_index[0].date if window == "all"
        else as_of - timedelta(days=_WINDOW_DAYS[window])
    )

    # Benchmark closes → reporting currency at daily carry-forward FX.
    #
    # §5.1(d) / W6c: the overlay REBASES this series to 100 at the window start, so every
    # point is divided by an earlier point — a split inside the window would show up as a
    # single-day index cliff of the whole ratio, next to a portfolio line that has none.
    #
    # ⚠ KNOWN LIMITATION, and the most likely place to meet it. ``ActionIndex`` is built
    # from the owner's RECORDED ledger actions, which are per account, and a benchmark is
    # deliberately NOT a registered instrument (see the module docstring) — nobody holds it,
    # so nobody records its splits. This call is therefore a structural no-op today: an
    # index whose real constituent split keeps the provider's own basis. There is no
    # independent split feed, and inferring one from a price gap is guessing, which
    # ``domain-ledger.md`` forbids — a heuristic here would silently rewrite market history.
    # The call is wired anyway because it is correct the moment such an action IS recorded,
    # and because leaving one series read raw is how the next reader concludes the rule is
    # optional.
    #
    # PERF spec §8.5 — a BOUNDED load. Every point the overlay reads is a carry-forward
    # lookup on a day in ``[window_start, as_of]``, so the closes are read from the last close
    # on or before ``window_start`` (``get_close_series``), and the FX from the last rate on
    # or before that first close — never from 1900. A first day on a weekend, a holiday or a
    # suspension keeps the value it carries forward.
    actions = load_action_index(conn)
    closes = closes_in(
        actions, bench.storage_key,
        get_close_series(conn, bench.storage_key, window_start, as_of),
        valued_on=as_of,
    )
    fx_history = _load_fx_history(conn, bench.quote_ccy, reporting,
                                  closes[0][0] if closes else window_start, as_of)
    bench_reporting = convert_closes(closes, fx_history, bench.quote_ccy, reporting)

    overlay = build_overlay(
        port_index, bench_reporting, window_start=window_start, window_end=as_of
    )
    if not overlay.available:
        return {**base, "available": False, "points": [], "reason": overlay.reason}

    return {
        **base, "available": True, "reason": None,
        "points": [
            {
                "date": p.date.isoformat(),
                "portfolio": _wire4(p.portfolio),
                "benchmark": _wire4(p.benchmark),
            }
            for p in overlay.points
        ],
    }
