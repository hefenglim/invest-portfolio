"""CLAUDE.md locked decision: "The frontend never computes money or returns."

Locked since 2026-06-13 and, until now, enforced by nothing. The cost of that showed up on
2026-08-05: ``web/app.js`` had kept ``f.signedPct(h.unrealized_pnl / h.adjusted_cost_total)``
in the holdings table for the eleven days since audit H1 moved the drawer onto the
server-computed ``unrealized_pct``. On a short position (negative basis by construction)
that divide inverted the sign and rendered a −75.98 USD loss as ``+3.17%``.

Two reasons the rule exists, both visible in that one line:
- **Sign.** The correct denominator is ``abs(original_cost_total)``; the backend knows that
  and carries the comment explaining why. A second implementation in JS does not inherit
  the reasoning, so it drifts silently.
- **Type.** ``h.unrealized_pnl`` is a Decimal STRING. ``/`` coerces it to an IEEE double —
  the exact thing "no money in floats" (invariant 3) forbids.

The field list is derived from the Pydantic wire models rather than hard-coded, so a new
Decimal field is covered the day it is added.
"""

from __future__ import annotations

import re
from decimal import Decimal  # noqa: F401  (referenced via the annotation string check)
from pathlib import Path

from pydantic import BaseModel

from portfolio_dash.portfolio import settlement_models
from portfolio_dash.portfolio.dashboard_models import (
    HoldingRow,
    KpiSummary,
    OptionContractRealized,
    OptionMoney,
    OptionObligationSubtotal,
    OptionOpenRow,
    OptionRealizedRow,
    OptionRealizedUnderlying,
    OptionRealizedYear,
    TrendPoint,
)

_WEB = Path(__file__).resolve().parents[2] / "web"


def _settlement_models() -> list[type[BaseModel]]:
    """Every wire model of the settlement contract (PERF spec §15): the status, the progress
    file, the run report and its rows (the per-day diff, restatements), and the period closes
    (KPI block, activity, holdings, cash) that 月度成績 and the panel render. Derived from the
    module, so a model added there is covered the day it lands."""
    return [obj for obj in vars(settlement_models).values()
            if isinstance(obj, type) and issubclass(obj, BaseModel)
            and obj.__module__ == settlement_models.__name__]


def _money_fields() -> set[str]:
    """Every Decimal-typed field on the dashboard and settlement wires — the values JS must not
    do math on."""
    names: set[str] = set()
    # The 選擇權 block (options spec §7.1, 2026-10-09): premiums, 口數, the obligation and the
    # option totals are server strings too — 口 ↔ 股當量 and every premium total included.
    # TrendPoint (the trend card, ``pnl`` included — owner ruling 2026-10-22) was not in the list
    # until then, which is how the tooltip's float subtraction went unseen until 2026-10-19, and
    # was then caught only because a settlement model happens to have a ``net_invested`` too.
    models: list[type[BaseModel]] = [
        HoldingRow, KpiSummary, OptionOpenRow, OptionObligationSubtotal, OptionContractRealized,
        OptionRealizedRow, OptionMoney, OptionRealizedYear, OptionRealizedUnderlying,
        TrendPoint, *_settlement_models()]
    for model in models:
        names |= {n for n, f in model.model_fields.items()
                  if "Decimal" in str(f.annotation)}
    return names


def _strip_noise(src: str) -> str:
    """Remove comments and string literals — a rule NAME inside a comment is not code, and
    the comments in this repo quote the very expressions being banned."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)//.*$", "", src)
    src = re.sub(r"'(?:[^'\\\n]|\\.)*'", "''", src)
    src = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', src)
    return src


def _violations() -> list[str]:
    """`x.<money_field>` adjacent to `*`, `/` or `-`.

    Two limits, stated rather than implied — a guard that oversells its coverage is worse
    than none:

    - `+` is NOT scanned. It is overwhelmingly string concatenation here, and a check that
      cries wolf gets deleted. The three operators scanned have no innocent reading on a
      Decimal string.
    - The scan is by FIELD NAME, so passing a field into a helper (`barWidth(h.weight, max)`)
      is invisible to it. That is deliberate: the legitimate remaining use — deriving a CSS
      bar length, a visual proportion rather than a displayed figure — takes exactly that
      shape, and the defects this catches were all inline derivations at the render site.
      What it guarantees is that no money VALUE is derived where it is printed.
    """
    out: list[str] = []
    for name, _lineno, line in _hits():
        if (name, line) in _KNOWN:
            continue
        out.append(f"web/{name}  {line[:90]}")
    return out


def _hits() -> list[tuple[str, int, str]]:
    """Every scanned line that does arithmetic next to a money field: (file, line in the
    comment-stripped text, the stripped line)."""
    fields = "|".join(sorted(_money_fields()))
    after = re.compile(rf"\.(?:{fields})\s*[*/-]\s*(?![/*])")   # h.foo / …  (not a comment)
    before = re.compile(rf"[*/-]\s*[A-Za-z_$][\w$]*\.(?:{fields})\b")
    out: list[tuple[str, int, str]] = []
    for path in sorted(_WEB.glob("*.js")):
        clean = _strip_noise(path.read_text(encoding="utf-8"))
        for lineno, line in enumerate(clean.splitlines(), start=1):
            if after.search(line) or before.search(line):
                out.append((path.name, lineno, line.strip()))
    return out


#: KNOWN violations, kept visible rather than hidden — each one a finding with an owner, not a
#: permission. (file, the stripped line) → why it is not fixed in place. Empty since 2026-10-22:
#: its one entry, the trend tooltip's ``pt.total_value - pt.net_invested`` (found 2026-10-19),
#: was fixed by the owner's ruling — the server sends each point's ``pnl`` and the tooltip only
#: formats it (``test_the_trend_tooltip_formats_the_servers_pnl``).
_KNOWN: dict[tuple[str, str], str] = {}


def test_no_js_arithmetic_on_a_money_field() -> None:
    violations = _violations()
    assert not violations, (
        "the frontend must render server-computed Decimal strings, never derive money or "
        "returns from them (CLAUDE.md locked decision; invariant 3):\n  "
        + "\n  ".join(violations)
    )


def test_every_known_violation_still_exists() -> None:
    """A known entry must not outlive its line: fixing it fails here until the entry goes."""
    seen = {(name, line) for name, _lineno, line in _hits()}
    gone = sorted(set(_KNOWN) - seen)
    assert not gone, f"known violations that no longer exist — delete their entry: {gone}"


def test_the_field_list_is_real_and_the_pattern_bites() -> None:
    """Detection power. A regex guard over a derived list has two ways to pass vacuously —
    an empty list, or a pattern that never matches — so pin both."""
    fields = _money_fields()
    assert {"unrealized_pnl", "adjusted_cost_total", "weight", "xirr"} <= fields, fields
    assert {"contracts", "share_equivalents", "premium_total", "obligation",
            "premium_in", "premium_out"} <= fields, fields
    # PERF spec §15: a period close's KPI block, the per-day diff and a restatement.
    assert {"total_market_value", "period_twr", "net_worth", "before", "after",
            "old_value", "new_value", "balance"} <= fields, fields
    # The trend card's points as served, ``pnl`` included (owner ruling 2026-10-22).
    assert {"total_value", "net_invested", "net_worth", "pnl"} <= fields, fields
    assert len(_settlement_models()) >= 20, _settlement_models()
    assert len(fields) >= 20, len(fields)

    names = "|".join(sorted(fields))
    after = re.compile(rf"\.(?:{names})\s*[*/-]\s*(?![/*])")
    # the exact line that shipped the bug
    assert after.search("f.signedPct(h.unrealized_pnl / h.adjusted_cost_total)")
    # and the trend tooltip's (2026-10-22), seen from either side of the operator — so a
    # reintroduced subtraction fails even if one of the two names left the list
    spread = "const spread = pt.total_value - pt.net_invested;"
    assert after.search(spread)
    assert re.search(rf"[*/-]\s*[A-Za-z_$][\w$]*\.(?:{names})\b", spread)
    assert after.search("const back = pt.pnl - 0;")
    assert after.search("(h.weight / maxWeight) * 100")
    # and things that must NOT trip it
    assert not after.search("tdPnl.appendChild(el('span', null, f.signed(h.unrealized_pnl)))")
    assert not after.search("const w = barWidth(h.weight, maxWeight);")


def test_comments_quoting_the_banned_expression_do_not_trip_it() -> None:
    """The fix's own comment names ``unrealized_pnl / adjusted_cost_total`` to explain what
    went wrong. Stripping comments is what lets the explanation live next to the code."""
    src = ("/* was: h.unrealized_pnl / h.adjusted_cost_total */\n"
           "const pct = h.unrealized_pct;\n"
           "// also h.weight / maxWeight\n")
    clean = _strip_noise(src)
    names = "|".join(sorted(_money_fields()))
    assert not re.search(rf"\.(?:{names})\s*[*/-]\s*(?![/*])", clean), clean


def test_the_trend_tooltip_formats_the_servers_pnl() -> None:
    """The positive half of the 2026-10-22 fix: it is not enough that the subtraction is gone.

    The tooltip's 累計總損益（含匯兌） must be the server's ``pnl`` string handed straight to
    ``fmt``, and its colour must come from the string's sign (``f.signClass``), not from a
    comparison that coerces it to a float."""
    clean = _strip_noise((_WEB / "charts.js").read_text(encoding="utf-8"))
    assert "f.signed(pt.pnl, " in clean, "the tooltip must format the server's pnl"
    assert "f.signClass(pt.pnl)" in clean, "the colour must read the sign off the string"
    assert not re.search(r"pt\.pnl\s*[<>]", clean), "a numeric comparison coerces the string"
    assert "pt.total_value - pt.net_invested" not in clean
