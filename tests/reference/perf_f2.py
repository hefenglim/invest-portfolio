"""PERF-F2's intended difference from the v0.1.31 reference payloads — ONE owner for every test
that compares against them — and the corpus variant that keeps a genuinely missing rate.

v0.1.31 (and v0.1.32) loaded the trend's FX only for the ledger's QUOTE currencies. On
``unquoted_ccy_cash`` — ``golden`` plus a MYR/TWD rate (7.05 on 2026-03-02) and a 50 MYR
``REBATE`` on 2026-03-16, a currency no instrument is quoted in — the rebate found no rate in
that map and the WHOLE trend was unavailable, with net worth, B, B − A, 交易與融資成本 and the
benchmark. The fix (``timeseries.trend_fx_currencies``) adds the currency of every cash movement
the trend converts. The reference payloads are never regenerated (``payloads/README.md``), so
this module states WHICH bytes the fix changes and what it changes them TO.

**The files.** ``unquoted_ccy_cash``'s ``dashboard.json`` and every ``month_end/<YYYY-MM>.json``
whose month end D is on or after the rebate (2026-03, 2026-04, 2026-05). Every other file of
every corpus — ``unquoted_ccy_cash``'s January and February month ends and its tax package
included — is v0.1.31's, byte for byte (:func:`expected_bytes` returns the file itself).

**The fields** (:data:`DASHBOARD_CHANGES`, :data:`MONTH_END_CHANGES` — exactly the paths that
differ, which ``test_reference_payloads`` checks both ways). Each is derived from ``golden``'s
own v0.1.31 file of the same name: until the rebate the two ledgers are the same, and from it
the only difference the fixed trend can see is the rebate itself.

- ``trend`` (``points``, ``available``): golden's series. From the rebate day on each point's
  ``net_invested`` is lowered by the rebate at ITS OWN date's rate (a credit among the
  trend's flows, AI-D48), and its ``net_worth`` raised by the 50 MYR pool at THAT DAY's
  carry-forward rate (the cash series; golden's MYR/TWD row of 2026-06-09, 7, takes over
  from 7.05). ``total_value`` and ``incomplete`` are golden's: the rebate holds no position.
- ``kpis.total_return_fx_complete`` (B) — golden's B plus the rebate: B is
  ``total_value − net_invested`` on the last day. ``kpis.trading_financing_cost`` — golden's
  plus the rebate (a credit is a positive P&L effect). ``kpis.principal_fx_effect`` —
  ``B − A − 交易與融資成本`` (AI-D48's three-term identity) with this corpus's own A, which the
  fix does not touch; null whenever one of them is. ``kpis.fx_complete_reason`` — golden's.
  A month end's B is withheld in both ledgers (a held price is missing at D), so there only
  the reason changes, from 「尚無趨勢資料…」 to golden's own.
- ``benchmark`` (``reason``, ``uncovered_markets``, ``uncovered_ratio``) — golden's block: a
  cash movement carries no market and is no flow of the counterfactual (AI-D49), and golden
  stores no index closes, so the counterfactual now refuses for want of index history (golden's
  answer) instead of for want of a rate.
- ``freshness.trend_unavailable_reason`` — golden's (null).

**The missing-rate variant** (:func:`drop_rebate_rate`): the same corpus with every MYR/TWD and
TWD/MYR row dated on or before the rebate deleted. The rebate then has NO rate on or before its
date — a missing rate, not a missing pair — and the series is unavailable from 2026-03-16 with
the reason 「帳本中有交易日期缺少匯率資料」, exactly as before the fix. The placeholder tests
use it.
"""

from __future__ import annotations

import copy
import functools
import json
import sqlite3
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from portfolio_dash.shared.wire import decimal_str
from tests.reference.corpora import (
    CORPORA,
    UNQUOTED_MOVE_AMOUNT,
    UNQUOTED_MOVE_DATE,
    open_db,
)
from tests.reference.payload_gen import canonical_json

PAYLOADS = Path(__file__).resolve().parent / "payloads"
CORPUS = "unquoted_ccy_cash"
#: The ledger the corpus adds its rebate to.
BASE = "golden"
#: The rebate's currency (no ledger instrument is quoted in it).
REBATE_CCY = "MYR"

#: Every path of ``dashboard.json`` the fix changes (dotted; a list is one value).
DASHBOARD_CHANGES: tuple[str, ...] = (
    "kpis.total_return_fx_complete",
    "kpis.principal_fx_effect",
    "kpis.trading_financing_cost",
    "kpis.fx_complete_reason",
    "trend.points",
    "trend.available",
    "benchmark.reason",
    "benchmark.uncovered_markets",
    "benchmark.uncovered_ratio",
    "freshness.trend_unavailable_reason",
)
#: Every path of an affected ``month_end`` file the fix changes: B, its split and the cost are
#: withheld at those month ends in both ledgers (a held price is missing at D).
MONTH_END_CHANGES: tuple[str, ...] = (
    "kpis.fx_complete_reason",
    "trend.points",
    "trend.available",
    "benchmark.reason",
    "benchmark.uncovered_markets",
    "benchmark.uncovered_ratio",
    "freshness.trend_unavailable_reason",
)

RateAt = Callable[[date], Decimal]


def is_affected(name: str, rel: str) -> bool:
    """Whether the fix changes reference file *rel* of corpus *name*."""
    if name != CORPUS:
        return False
    if rel == "dashboard.json":
        return True
    if rel.startswith("month_end/"):
        return _month_end_day(rel) >= UNQUOTED_MOVE_DATE
    return False


def changes(rel: str) -> tuple[str, ...]:
    """The paths of affected file *rel* the fix changes."""
    return DASHBOARD_CHANGES if rel == "dashboard.json" else MONTH_END_CHANGES


def v0131_bytes(name: str, rel: str) -> bytes:
    """The reference file exactly as v0.1.31's code wrote it."""
    return (PAYLOADS / name / rel).read_bytes()


def expected_bytes(name: str, rel: str) -> bytes:
    """What the fixed code must render for reference file *rel* of corpus *name*: v0.1.31's
    bytes, except on the affected files, where the :data:`DASHBOARD_CHANGES` /
    :data:`MONTH_END_CHANGES` paths carry the values derived from ``golden`` (module
    docstring) and every other byte is v0.1.31's."""
    raw = v0131_bytes(name, rel)
    if not is_affected(name, rel):
        return raw
    dump = _dump_dashboard if rel == "dashboard.json" else canonical_json
    v0131 = json.loads(raw)
    # guard the guard: the serializer reproduces the v0.1.31 file itself, so every byte the
    # derivation does not replace is v0.1.31's
    assert dump(v0131) == raw, f"{name}/{rel}: the serializer does not round-trip the file"
    golden = json.loads(v0131_bytes(BASE, rel))
    return dump(expected_document(v0131, golden, _rate_at))


def expected_document(v0131: dict[str, Any], golden: dict[str, Any],
                      rate_at: RateAt) -> dict[str, Any]:
    """The affected document as the fixed code renders it (see the module docstring)."""
    doc = copy.deepcopy(v0131)
    flow = UNQUOTED_MOVE_AMOUNT * rate_at(UNQUOTED_MOVE_DATE)  # the rebate, at its own date
    gk, k = golden["kpis"], doc["kpis"]
    b = _plus(gk["total_return_fx_complete"], flow)
    cost = _plus(gk["trading_financing_cost"], flow)
    a = k["total_return"]
    k["total_return_fx_complete"] = b
    k["trading_financing_cost"] = cost
    k["principal_fx_effect"] = (
        None if b is None or a is None or cost is None
        else decimal_str(Decimal(b) - Decimal(a) - Decimal(cost)))
    k["fx_complete_reason"] = gk["fx_complete_reason"]
    trend = copy.deepcopy(golden["trend"])
    trend["points"] = [_shift(p, flow, rate_at) for p in trend["points"]]
    doc["trend"] = trend
    doc["benchmark"] = copy.deepcopy(golden["benchmark"])
    doc["freshness"]["trend_unavailable_reason"] = (
        golden["freshness"]["trend_unavailable_reason"])
    return doc


def changed_paths(a: Any, b: Any, prefix: str = "") -> list[str]:
    """The dotted paths where two parsed documents differ: dict keys are recursed into (a
    changed key ORDER is reported as ``<path> <keys>``), any other value — a list included —
    is compared whole."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = [f"{prefix} <keys>"] if list(a) != list(b) else []
        for key in a:
            if key in b:
                out += changed_paths(a[key], b[key], f"{prefix}.{key}" if prefix else key)
        return out
    return [] if a == b else [prefix]


def drop_rebate_rate(path: Path) -> None:
    """The missing-rate variant of the corpus at *path*: delete every MYR/TWD and TWD/MYR row
    dated on or before the rebate, so the rebate has no rate on or before its date. Run it on
    a private copy BEFORE the store's triggers are installed (``prepare``)."""
    conn = open_db(path)
    try:
        cur = conn.execute(
            "DELETE FROM fx_rates WHERE ((base = ? AND quote = ?) OR (base = ? AND quote = ?)) "
            "AND as_of_date <= ?",
            (REBATE_CCY, _reporting(), _reporting(), REBATE_CCY,
             UNQUOTED_MOVE_DATE.isoformat()))
        assert cur.rowcount > 0, f"{path.name}: no rate on or before the rebate to drop"
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------------


def _reporting() -> str:
    return CORPORA[CORPUS].reporting.value


def _month_end_day(rel: str) -> date:
    """The month end D of ``month_end/<YYYY-MM>.json``."""
    year, month = (int(x) for x in rel.removeprefix("month_end/").removesuffix(".json")
                   .split("-"))
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def _plus(text: str | None, amount: Decimal) -> str | None:
    return None if text is None else decimal_str(Decimal(text) + amount)


def _shift(point: dict[str, Any], flow: Decimal, rate_at: RateAt) -> dict[str, Any]:
    day = date.fromisoformat(point["date"])
    if day < UNQUOTED_MOVE_DATE:
        return point
    out = dict(point)
    out["net_invested"] = decimal_str(Decimal(point["net_invested"]) - flow)
    if point["net_worth"] is not None:
        out["net_worth"] = decimal_str(Decimal(point["net_worth"])
                                       + UNQUOTED_MOVE_AMOUNT * rate_at(day))
    return out


@functools.cache
def _rates() -> dict[bool, tuple[tuple[date, Decimal], ...]]:
    """The corpus's MYR/TWD (``False``) and TWD/MYR (``True``) rows, ascending — seeded once
    into a private in-memory database by the corpus's own seed function."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row  # as ``corpora.open_db`` opens a template
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        CORPORA[CORPUS].seed(conn)
        return {inverse: tuple(
            (date.fromisoformat(r[0]), Decimal(r[1])) for r in conn.execute(
                "SELECT as_of_date, rate FROM fx_rates WHERE base = ? AND quote = ? "
                "ORDER BY as_of_date", pair))
            for inverse, pair in ((False, (REBATE_CCY, _reporting())),
                                  (True, (_reporting(), REBATE_CCY)))}
    finally:
        conn.close()


def _rate_at(day: date) -> Decimal:
    """MYR → the reporting currency on or before *day*, straight from the corpus's
    ``fx_rates``: the direct pair, else ``1 /`` the inverse — an independent transcription of
    the carry-forward rule the trend and the cash series apply."""
    for inverse, rows in sorted(_rates().items()):
        before = [rate for d, rate in rows if d <= day]
        if before:
            return Decimal(1) / before[-1] if inverse else before[-1]
    raise AssertionError(f"no {REBATE_CCY} rate on or before {day}")


def _dump_dashboard(doc: Any) -> bytes:
    """``GET /api/dashboard``'s body form: FastAPI's ``JSONResponse``."""
    return json.dumps(doc, ensure_ascii=False, allow_nan=False, indent=None,
                      separators=(",", ":")).encode("utf-8")


__all__ = [
    "CORPUS",
    "DASHBOARD_CHANGES",
    "MONTH_END_CHANGES",
    "changed_paths",
    "changes",
    "drop_rebate_rate",
    "expected_bytes",
    "expected_document",
    "is_affected",
    "v0131_bytes",
]
