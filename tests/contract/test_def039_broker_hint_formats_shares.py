"""DEF-039 (functional test manual I-10, 2026-09-24): the 券商對帳單 page SAYS share counts
through the share formatter — and still WRITES the exact figure.

DEF-027 made the opening-gap hint read the ledger's own holding (``ledger_shares``), which is
the replayed position: a DRIP quotient carried at full Decimal precision. The page printed it
verbatim — 「帳本已有 85.03925507380073800738007380 股」 — beside a correct gap of 914.96.

This file RUNS the shipped ``renderNeedsInput`` / ``completedOpeningsCsv`` from
``web/broker-import.js`` (Node — Playwright's driver — with the real ``web/format.js``) against
a minimal DOM stub, feeding it the measured payload. It asserts both halves of the contract:
the sentence shows 6-dp-trimmed shares, and the openings CSV the page commits still carries the
server's exact gap string (a formatted number must never be what gets written).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

_WEB = Path(__file__).resolve().parents[2] / "web"
_SRC = (_WEB / "broker-import.js").read_text(encoding="utf-8")
#: The helpers + constants block of broker-import.js ends here (el, fmtShares, TYPE_ZH, …).
_PREFIX_END = "  // ---------------------------------------------------------------- source switch"

_LEDGER = "85.03925507380073800738007380"
_GAP = "914.96074492619926199261992620"


def _node() -> Path | None:
    import playwright

    for name in ("node.exe", "node"):
        node = Path(playwright.__file__).parent / "driver" / name
        if node.exists():
            return node
    return None


def _function(name: str) -> str:
    """`function name(…) { … }` sliced out of broker-import.js: from its declaration to the
    first line that is exactly the IIFE-level closing brace (`  }`) — every top-level function
    in the file is indented two spaces, so that line is its own end."""
    start = _SRC.index(f"\n  function {name}(") + 1
    close = "\n  }\n"
    end = _SRC.index(close, start) + len(close)
    return _SRC[start:end]


_DOM = r"""
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this._t = ''; this.style = {};
    this.id = ''; this.value = ''; this.placeholder = ''; this.className = ''; }
  set textContent(v) { this._t = String(v); this.children = []; }
  get textContent() { return this._t + this.children.map((c) => c.textContent).join(''); }
  appendChild(n) { this.children.push(n); return n; }
  addEventListener() {}
  *walk() { yield this; for (const c of this.children) yield* c.walk(); }
}
const root = new Node('div');
const account = new Node('select'); account.id = 'bk-account'; account.value = 'schwab';
root.appendChild(account);
const document = {
  createElement: (t) => new Node(t),
  querySelector: (sel) => { for (const n of root.walk()) if ('#' + n.id === sel) return n;
                            return null; },
};
const window = {};
"""


def _run(openings: list[dict[str, Any]]) -> dict[str, Any]:
    node = _node()
    if node is None:
        pytest.skip("no Node interpreter (Playwright driver) in this venv")
    conv = {"openings_needing_cost": openings, "actions_needing_input": [],
            "openings_build_date": "2026-01-01",
            "worksheet_headers": {"openings": "account,symbol,shares,cost,build_date,note"}}
    code = (
        _DOM
        + "(function () {\n" + (_WEB / "format.js").read_text(encoding="utf-8") + "\n})();\n"
        + "const out = " + _SRC[:_SRC.index(_PREFIX_END)]
        + _function("renderNeedsInput") + _function("completedOpeningsCsv")
        + _function("csvCell")
        + "  conversion = " + json.dumps(conv) + ";\n"
        + "  const box = new Node('div'); root.appendChild(box);\n"
        + "  renderNeedsInput(box, conversion);\n"
        + "  const cost = document.querySelector('#bk-opening-cost-0');\n"
        + "  if (cost) cost.value = '90000';\n"
        + "  const ph = cost ? cost.placeholder : null;\n"
        + "  return { text: box.textContent, placeholder: ph, csv: completedOpeningsCsv() };\n"
        + "})();\n"
        + "process.stdout.write(JSON.stringify(out));\n"
    )
    done = subprocess.run([str(node), "-"], input=code, capture_output=True, timeout=30,
                          encoding="utf-8", check=False)
    assert done.returncode == 0, done.stderr
    result: dict[str, Any] = json.loads(done.stdout)
    return result


def test_the_gap_sentence_shows_formatted_shares_and_the_csv_keeps_the_exact_gap() -> None:
    got = _run([{"symbol": "AAPL", "shares": "1000", "ledger_shares": _LEDGER, "gap": _GAP,
                 "as_of": "2025-12-31", "satisfied": False}])
    text = got["text"]
    assert "帳本在 2025-12-31 已有 85.039255 股" in text, text
    assert "仍缺 914.960745 股" in text and "對帳單需要 1,000 股" in text, text
    assert _LEDGER not in text and _GAP not in text   # never the 28-decimal wire string
    assert got["placeholder"] == "這 914.960745 股當初買進的總金額（含手續費與稅）"
    # What the page WRITES is the server's exact gap, not the formatted one.
    assert f"schwab,AAPL,{_GAP},90000,2026-01-01," in got["csv"], got["csv"]


def test_a_covered_position_says_its_holding_formatted_too() -> None:
    got = _run([{"symbol": "AAPL", "shares": "80", "ledger_shares": _LEDGER, "gap": None,
                 "as_of": "2025-12-31", "satisfied": True}])
    assert "已持有 85.039255 股，足以涵蓋這份對帳單需要的 80 股" in got["text"], got["text"]
    assert got["csv"] == ""   # a covered position writes no opening row


# The class (DEF-039 scan, 2026-09-24): the other wire Decimals the scan found spliced into a
# sentence verbatim. Their surfaces are not unit-runnable one by one (toast / tooltip / confirm
# bodies inside larger render functions), so the class is held here statically — any of these
# fields concatenated into text without a formatter call fails, on any page.
_RAW_SPLICE = r"['\"]\s*\+\s*[A-Za-z_$][\w$.]*\.(%s)\b(?!\s*\()"
_FIELDS = ("ledger_shares", "gap", "per_share", "target_low", "target_high", "close", "last")


def test_no_page_splices_these_wire_decimals_into_text_unformatted() -> None:
    import re

    rx = re.compile(_RAW_SPLICE % "|".join(_FIELDS))
    offenders = [
        f"{p.name}:{n}: {line.strip()}"
        for p in sorted(_WEB.glob("*.js")) if not p.name.endswith(".min.js")
        for n, line in enumerate(p.read_text(encoding="utf-8").split("\n"), 1)
        if rx.search(line) and not line.strip().startswith(("//", "*", "/*"))
    ]
    assert not offenders, "\n".join(offenders)
    assert rx.search("parts.push('股價 ' + m.close);")          # the guard bites
    assert not rx.search("parts.push('股價 ' + f.exact(m.close));")
