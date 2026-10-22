"""The trend points' ``pnl`` (owner rulings 2026-10-22) on top of the v0.1.31 reference
payloads — ONE owner for every test that compares a SERVED payload against them.

The trend tooltip's 累計總損益（含匯兌） used to be ``total_value − net_invested`` subtracted in JS
floats (``web/charts.js``). The owner ruled that the server sends it instead: every trend point
gains one key, ``pnl``, appended after ``net_worth`` — that point's ``total_value −
net_invested`` at the reporting currency's minor unit (TWD 0 dp, USD / MYR 2 dp), ROUND_HALF_UP,
a rounded-away loss reading ``0`` rather than ``-0`` — and ``null`` on a point the file itself
marks ``incomplete`` (the second ruling: an unreliable figure is not shown, as the KPI B gate
withholds B). The reference files are never regenerated (``payloads/README.md``), so this
module states WHICH bytes the field changes and what it changes them TO.

**Composed with PERF-F2, never instead of it.** :func:`expected_bytes` starts from
``perf_f2.expected_bytes`` (v0.1.31's bytes, with PERF-F2's intended difference on
``unquoted_ccy_cash``) and adds ``pnl`` to every point of ``trend.points``. Every other byte of
every file is PERF-F2's expectation: the tax package and a file with no trend points are
returned as they are.

**Derived independently.** Each value is computed HERE from the file's own ``total_value`` /
``net_invested`` strings, its own ``incomplete`` flag and its own ``trend.reporting_currency``,
with a transcription of the
rounding rule (:data:`_MINOR`, ROUND_HALF_UP) rather than a call into the code under test
(``portfolio.dashboard_models.trend_pnl``), so the comparison cannot pass by agreeing
with itself.

**Who uses which.** A test that compares what the API SERVES (the dashboard body, a cache hit,
the store's read path) reads :func:`expected_bytes`. A test that compares what the settlement
WORKER stores (a settled day, a stored close) keeps ``perf_f2``: ``pnl`` is derived when a series
is served and is never stored, so the store's rows are unchanged.
"""

from __future__ import annotations

import copy
import json
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from tests.reference import perf_f2
from tests.reference.payload_gen import canonical_json

#: The wire key (the field ``portfolio.dashboard_models.TrendPoint.pnl``), spelled out here.
KEY = "pnl"
#: Display minor units per reporting currency — a transcription of
#: ``shared/money.MINOR_UNITS``, not an import of it.
_MINOR: dict[str, int] = {"TWD": 0, "USD": 2, "MYR": 2}


def carries_points(rel: str) -> bool:
    """Whether reference file *rel* holds trend points (the dashboard and every month end)."""
    return rel == "dashboard.json" or rel.startswith("month_end/")


def point_pnl(total_value: str, net_invested: str, reporting: str, *,
              incomplete: bool) -> str | None:
    """One point's ``pnl`` as the wire carries it, from the point's own strings and flag:
    ``None`` (JSON ``null``) on an incomplete day."""
    if incomplete:
        return None
    exact = Decimal(total_value) - Decimal(net_invested)
    shown = exact.quantize(Decimal(1).scaleb(-_MINOR[reporting]), rounding=ROUND_HALF_UP)
    if shown.is_zero():
        shown = shown.copy_abs()  # "-0" is noise, not a loss
    return format(shown, "f")


def with_pnl(doc: dict[str, Any]) -> dict[str, Any]:
    """*doc* (a parsed dashboard or month-end payload) with ``pnl`` appended to every trend
    point; nothing else is touched."""
    out = copy.deepcopy(doc)
    trend = out.get("trend")
    if not isinstance(trend, dict):
        return out
    reporting = trend["reporting_currency"]
    for point in trend.get("points", []):
        assert KEY not in point, f"a v0.1.31 point already has {KEY!r}: {point}"
        point[KEY] = point_pnl(point["total_value"], point["net_invested"], reporting,
                               incomplete=point["incomplete"])
    return out


def expected_bytes(name: str, rel: str) -> bytes:
    """What the current code must SERVE for reference file *rel* of corpus *name*:
    ``perf_f2.expected_bytes`` with ``pnl`` on every trend point (module docstring)."""
    raw = perf_f2.expected_bytes(name, rel)
    if not carries_points(rel):
        return raw
    dump = dump_dashboard if rel == "dashboard.json" else canonical_json
    doc = json.loads(raw)
    # guard the guard: the serializer reproduces the file, so every byte the derivation does
    # not add is PERF-F2's expectation
    assert dump(doc) == raw, f"{name}/{rel}: the serializer does not round-trip the file"
    return dump(with_pnl(doc))


def dump_dashboard(doc: Any) -> bytes:
    """``GET /api/dashboard``'s body form: FastAPI's ``JSONResponse``."""
    return json.dumps(doc, ensure_ascii=False, allow_nan=False, indent=None,
                      separators=(",", ":")).encode("utf-8")


__all__ = [
    "KEY",
    "carries_points",
    "dump_dashboard",
    "expected_bytes",
    "point_pnl",
    "with_pnl",
]
