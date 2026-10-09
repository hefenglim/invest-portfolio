"""The backend prints a USD amount ONE way: ``shared.money.usd_display``.

Owner ruling 2026-09-30 on the verifier's R11 note: ``strategy/alerts.py:154-156`` kept a
local ``_usd`` for the 「AI 額度偏低」 alert detail — ``f"${x.quantize(Decimal('0.01'))}"``:
the context's half-even rounding, no thousands separator, and ``-0.001`` printed as
「$-0.00」 — while DEF-085 had moved the dry-run gate, the pipeline node and the budget
refusal onto ``usd_display`` (half-up, 「$1,234.50」, 「$0.00」).

Why DEF-085's check did not see it: its tests pinned the three surfaces it had fixed, by
name — a list, not a scan. The scan below reads every f-string in the package and fails on a
literal ``$`` directly in front of a formatted value that is not ``usd_display(...)``.
"""

import ast
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import portfolio_dash
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.shared.enums import Currency
from portfolio_dash.strategy.alerts import compute_alerts_from
from portfolio_dash.strategy.rules_config import DEFAULT_RULES

_PKG = Path(portfolio_dash.__file__).resolve().parent
_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))

#: ``$`` before a value that is not a USD amount — each with why.
_NOT_USD = {
    # the password hash's own field separators: 「scrypt$<salt>$<key>」
    ("api/auth_store.py", "crypt$"),
    ("api/auth_store.py", "$"),
    # a TW fee rule's minimum, in NT$ (TWD) — a different currency, printed as configured
    ("api/wire.py", "最低 NT$"),
    # usd_display itself: 「$」 + the 2-dp, thousands-separated amount
    ("shared/money.py", "$"),
}


def _dollar_sites() -> set[tuple[str, str]]:
    """Every f-string ``$`` immediately followed by a value that is not ``usd_display``."""
    sites: set[tuple[str, str]] = set()
    for path in sorted(_PKG.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(_PKG).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.JoinedStr):
                continue
            parts = node.values
            for before, value in zip(parts, parts[1:], strict=False):
                if not (isinstance(before, ast.Constant) and isinstance(before.value, str)
                        and before.value.endswith("$")
                        and isinstance(value, ast.FormattedValue)):
                    continue
                call = value.value
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                        and call.func.id == "usd_display"):
                    continue
                sites.add((rel, before.value[-6:].lstrip()))
    return sites


def test_no_f_string_prints_a_dollar_amount_of_its_own() -> None:
    assert _dollar_sites() == _NOT_USD


def test_usd_display_is_the_one_that_writes_the_dollar_sign() -> None:
    """``usd_display`` itself is the only ``$`` + format spec in the package."""
    src = (_PKG / "shared" / "money.py").read_text(encoding="utf-8")
    assert 'return f"${q:,.2f}"' in src


@pytest.mark.parametrize("remaining, threshold, shown", [
    # half-up at the cent, as the gate and the node print it (half-even said $0.12)
    ("0.125", "1", "剩餘額度 $0.13＜警戒值 $1.00"),
    # an overdraft smaller than a cent is not 「$-0.00」
    ("-0.001", "1", "剩餘額度 $0.00＜警戒值 $1.00"),
    # H-05's balance, as the dry run's R6 and the pipeline node print it
    ("-0.01", "1", "剩餘額度 $-0.01＜警戒值 $1.00"),
    # thousands separator on a large threshold
    ("999.5", "1234.5", "剩餘額度 $999.50＜警戒值 $1,234.50"),
])
def test_the_quota_alert_prints_the_amounts_like_every_other_surface(
    golden_db: sqlite3.Connection, remaining: str, threshold: str, shown: str
) -> None:
    data = build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    alerts = compute_alerts_from(data, DEFAULT_RULES, quota_remaining=Decimal(remaining),
                                 quota_threshold=Decimal(threshold))
    assert next(a for a in alerts if a.id == "quota_low").detail == shown
