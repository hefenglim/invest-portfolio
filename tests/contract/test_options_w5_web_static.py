"""Options W5 — static wiring of the web surfaces (spec §7, §8).

Each of these is a one-line fact a page depends on, and each failed once in this repo's
history in its own shape (a script nobody loaded, a refusal reason the page had no sentence
for, a fetch-layer field the page read and never received):

* the broker page has a remedy for EVERY option reason the converter can stop on (§8) — a
  reason with no sentence would fall back to 「請把上面那行訊息回報」, which tells the owner to
  report a refusal that has a documented fix;
* ``web/api.js`` carries the 422 envelope's ``reason`` and ``line`` onto the error;
* the dashboard loads ``options.js`` and the inbox page loads ``expiry-inbox.js``;
* the sidebar badge counts the expiry inbox (spec §7.2: 「the sidebar badge adds its count」).
"""

from __future__ import annotations

import re
from pathlib import Path

from portfolio_dash.data_ingestion.broker import ir

_WEB = Path(__file__).resolve().parents[2] / "web"


def _src(name: str) -> str:
    return (_WEB / name).read_text(encoding="utf-8")


def test_every_option_unmapped_reason_has_a_remedy() -> None:
    reasons = {ir.OPTION_ASSIGNED_UNMAPPED, ir.OPTION_NONSTANDARD_ADJUST,
               ir.OPTION_EXPIRY_NO_OPENING}
    block = re.search(r"const UNMAPPED_REMEDY = \{(.*?)\n  \};", _src("broker-import.js"), re.S)
    assert block, "web/broker-import.js lost UNMAPPED_REMEDY"
    keys = set(re.findall(r"^\s{4}(\w+):", block.group(1), re.M))
    assert keys == reasons


def test_the_fetch_layer_carries_reason_and_line() -> None:
    api = _src("api.js")
    assert "reason = err.reason" in api and "line = err.line" in api
    assert "out.reason = reason" in api and "out.line = line" in api


def test_the_pages_load_their_option_scripts() -> None:
    assert re.search(r'<script src="options\.js\?v=[^"]+"></script>', _src("index.html"))
    assert re.search(r'<script src="expiry-inbox\.js\?v=[^"]+"></script>',
                     _src("dividend-inbox.html"))


def test_the_sidebar_badge_counts_the_expiry_inbox() -> None:
    assert "'/api/option-expiry/count'" in _src("shell.js")
