"""M9-01: 「還原預設值」 must restore EVERY alert rule, not the ones a constant table remembers.

The reset button does not call a reset endpoint — none exists. It PUTs a hand-written
default body from ``web/settings-alerts.js``'s ``DEFAULTS_WIRE``, and
``PUT /api/alert-rules`` is a **subset merge**: a rule absent from the body keeps its
CURRENT value. So a rule missing from that table is silently NOT restored, and the button
still reports 「✓ 已還原預設值」.

That is exactly what happened. ``portfolio_drawdown`` and ``currency_weight`` were added to
``strategy/rules_config.py`` (R5) and never to the frontend table, so a reset sent 13 of 15
ids and left both thresholds at whatever the user had typed — including values that decide
whether the bell fires at all.

Nothing could catch it: an omitted key is not a type error, not a 4xx, and not visible in
the response (the PUT echoes the merged rules, which look perfectly well-formed). The only
thing that can see it is a comparison against the OTHER owner of the defaults.

So this test pins the frontend mirror to ``RULE_META``. The backend stays the single
authority for the numbers; the mirror is allowed to exist only while it is provably equal.
Add a rule to ``RULE_META`` without adding it here and this goes red on the next run,
which is the difference between a mirror and a second owner.
"""

import re
from decimal import Decimal
from pathlib import Path

from portfolio_dash.strategy.rules_config import DEFAULT_RULES, RULE_IDS, RULE_META

_JS = Path(__file__).resolve().parents[2] / "web" / "settings-alerts.js"

_ARRAY = re.compile(r"DEFAULTS_WIRE\s*=\s*\[(.*?)\];", re.S)
_ENTRY = re.compile(
    r"\{\s*id:\s*'([^']+)'\s*,\s*enabled:\s*(true|false)\s*,"
    r"\s*value:\s*(null|'[^']*')\s*\}"
)


def _defaults_wire() -> list[tuple[str, bool, str | None]]:
    """Parse the ``DEFAULTS_WIRE`` array literal the reset button PUTs."""
    block = _ARRAY.search(_JS.read_text(encoding="utf-8"))
    assert block is not None, "DEFAULTS_WIRE not found in web/settings-alerts.js"
    out: list[tuple[str, bool, str | None]] = []
    for rid, enabled, value in _ENTRY.findall(block.group(1)):
        out.append((rid, enabled == "true", None if value == "null" else value.strip("'")))
    return out


def test_the_parse_finds_the_table_it_guards() -> None:
    """A guard that matches nothing passes forever."""
    parsed = _defaults_wire()
    assert len(parsed) >= 13, f"only {len(parsed)} entries parsed — has the literal changed?"


def test_reset_sends_every_backend_rule_id() -> None:
    """The load-bearing one: a subset merge restores only what the body carries."""
    sent = [rid for rid, _en, _v in _defaults_wire()]
    assert len(sent) == len(set(sent)), f"duplicate ids in DEFAULTS_WIRE: {sent}"
    missing = [rid for rid in RULE_IDS if rid not in sent]
    unknown = [rid for rid in sent if rid not in RULE_META]
    assert not missing, (
        "還原預設值 would silently NOT restore these rules — PUT /api/alert-rules is a "
        f"subset merge, so an omitted id keeps its current value: {missing}"
    )
    assert not unknown, f"DEFAULTS_WIRE names rules the backend does not have: {unknown}"


def test_reset_values_equal_the_backend_defaults() -> None:
    """Covering every id is not enough if the number sent is not the default."""
    wrong: list[str] = []
    for rid, enabled, value in _defaults_wire():
        want = getattr(DEFAULT_RULES, rid)
        if enabled is not want.enabled:
            wrong.append(f"{rid}: enabled={enabled} but default is {want.enabled}")
            continue
        if want.value is None:
            if value is not None:
                wrong.append(f"{rid}: sends {value!r} but the rule is toggle-only")
        elif value is None or Decimal(value) != want.value:
            wrong.append(f"{rid}: sends {value!r} but the default is {want.value}")
    assert not wrong, (
        "還原預設值 sends values that are not the backend defaults:\n" + "\n".join(wrong)
    )
