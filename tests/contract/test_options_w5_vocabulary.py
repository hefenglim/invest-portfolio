"""Options spec §6.1 — the browser's copy of the option vocabulary is ONE table, and it is the
server's.

The wire carries most §6.1 words as server strings (``option_side``, ``closed_how``, the
drawer's ``*_label``), and the pages print them as they came. Three things arrive as CODES and
need a word in the browser — ``direction`` (long / short), ``right`` (C / P) and
``close_reason`` — plus the §4.4 remedy beside a refused close on the 交易 tab, whose rows
carry only the ``refused_close`` flag. Those live in ONE place, ``window.pdOptionWords`` in
``web/format.js``; this file pins every entry to ``shared/options.py`` (a JS table nobody pins
drifts — the corporate-action kinds had four private copies before I-14), and fails on a
second code → word table anywhere else in ``web/``.

The manual form's side buttons (``web/trades.html``) are static labels sent as codes; they are
pinned to ``OptionSide`` here too.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from portfolio_dash.shared.options import (
    CLOSE_REASON_ZH,
    DIRECTION_ZH,
    REFUSED_CLOSE_REMEDY,
    RIGHT_ZH,
    UNIT_CONTRACT,
    UNIT_SHARE_EQUIVALENT,
    OptionSide,
)

_WEB = Path(__file__).resolve().parents[2] / "web"


def _node() -> Path | None:
    import playwright

    for name in ("node.exe", "node"):
        node = Path(playwright.__file__).parent / "driver" / name
        if node.exists():
            return node
    return None


def _browser_words() -> dict[str, object]:
    node = _node()
    if node is None:
        pytest.skip("no node interpreter (Playwright driver) to evaluate web/format.js")
    script = (
        "global.window = {}; global.document = {};\n"
        + (_WEB / "format.js").read_text(encoding="utf-8")
        + "\nprocess.stdout.write(JSON.stringify(window.pdOptionWords));\n"
    )
    out = subprocess.run([str(node), "-e", script], capture_output=True, text=True,
                         encoding="utf-8", check=True, timeout=60)
    words: dict[str, object] = json.loads(out.stdout)
    return words


def test_the_browser_words_are_the_servers() -> None:
    w = _browser_words()
    assert w["direction"] == DIRECTION_ZH
    assert w["right"] == RIGHT_ZH
    assert w["closeReason"] == CLOSE_REASON_ZH
    assert w["unit"] == UNIT_CONTRACT
    assert w["shareEq"] == UNIT_SHARE_EQUIVALENT
    assert w["refusedRemedy"] == REFUSED_CLOSE_REMEDY
    assert w["unbooked"] == OptionSide.UNBOOKED.value


#: `long: '買方'` / `STO: '賣出開倉'` / `expired: '到期作廢'` — an option code paired with a §6.1
#: word, i.e. a private code → word table.
_WORDS = sorted({*DIRECTION_ZH.values(), *RIGHT_ZH.values(), *CLOSE_REASON_ZH.values(),
                 *(s.value for s in OptionSide)}, key=len, reverse=True)
_PAIR = re.compile(
    r"\b(long|short|C|P|STO|BTC|BTO|STC|expired|assigned|exercised)['\"]?\s*:\s*['\"]("
    + "|".join(re.escape(w) for w in _WORDS) + r")['\"]")


def _pairs() -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {}
    for path in sorted(_WEB.glob("*.js")):
        if path.name.endswith(".min.js"):
            continue
        found = _PAIR.findall(path.read_text(encoding="utf-8"))
        if found:
            out[path.name] = found
    return out


def test_no_other_web_file_keeps_its_own_option_table() -> None:
    offenders = {k: v for k, v in _pairs().items() if k != "format.js"}
    assert not offenders, (
        "a web file maps option codes to §6.1 words itself — read window.pdOptionWords "
        f"(web/format.js) or print the server's string instead: {offenders}")
    assert "format.js" in _pairs(), "the guard is blind: format.js's own table no longer matches"


def test_the_guard_bites() -> None:
    assert _PAIR.findall("const D = { long: '買方', short: '賣方' };") == [
        ("long", "買方"), ("short", "賣方")]
    assert _PAIR.findall("x = { STO: '賣出開倉' }") == [("STO", "賣出開倉")]


def test_the_side_buttons_send_the_codes_the_server_reads() -> None:
    html = (_WEB / "trades.html").read_text(encoding="utf-8")
    intents = dict(re.findall(r'data-intent="(STO|BTC|BTO|STC)">([^<]+)<', html))
    assert intents == {"STO": OptionSide.STO.value, "BTC": OptionSide.BTC.value,
                       "BTO": OptionSide.BTO.value, "STC": OptionSide.STC.value}
    closes = dict(re.findall(r'data-close="(\w+)">([^<]+)<', html))
    assert closes == {"expired": OptionSide.EXPIRED.value,
                      "assign": OptionSide.ASSIGNED.value + "／" + OptionSide.EXERCISED.value}
