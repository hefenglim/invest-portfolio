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

from portfolio_dash.portfolio.dashboard_models import HoldingRow, KpiSummary

_WEB = Path(__file__).resolve().parents[2] / "web"


def _money_fields() -> set[str]:
    """Every Decimal-typed field on the dashboard wire — the values JS must not do math on."""
    names: set[str] = set()
    for model in (HoldingRow, KpiSummary):
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
    fields = "|".join(sorted(_money_fields()))
    after = re.compile(rf"\.(?:{fields})\s*[*/-]\s*(?![/*])")   # h.foo / …  (not a comment)
    before = re.compile(rf"[*/-]\s*[A-Za-z_$][\w$]*\.(?:{fields})\b")
    out: list[str] = []
    for path in sorted(_WEB.glob("*.js")):
        clean = _strip_noise(path.read_text(encoding="utf-8"))
        for lineno, line in enumerate(clean.splitlines(), start=1):
            if after.search(line) or before.search(line):
                out.append(f"web/{path.name}:{lineno}  {line.strip()[:90]}")
    return out


def test_no_js_arithmetic_on_a_money_field() -> None:
    violations = _violations()
    assert not violations, (
        "the frontend must render server-computed Decimal strings, never derive money or "
        "returns from them (CLAUDE.md locked decision; invariant 3):\n  "
        + "\n  ".join(violations)
    )


def test_the_field_list_is_real_and_the_pattern_bites() -> None:
    """Detection power. A regex guard over a derived list has two ways to pass vacuously —
    an empty list, or a pattern that never matches — so pin both."""
    fields = _money_fields()
    assert {"unrealized_pnl", "adjusted_cost_total", "weight", "xirr"} <= fields, fields
    assert len(fields) >= 20, len(fields)

    names = "|".join(sorted(fields))
    after = re.compile(rf"\.(?:{names})\s*[*/-]\s*(?![/*])")
    # the exact line that shipped the bug
    assert after.search("f.signedPct(h.unrealized_pnl / h.adjusted_cost_total)")
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
