"""DEF-023, R2 bounce (functional test manual D-11): the dashboard's 「公司行動無法套用」 block
must RENDER on a ledger with no unregistered symbol.

The R2 fix added ``renderUnappliedBanner()`` to ``web/app.js`` but called it from inside
``renderUnregisteredBanner()`` AFTER that function's ``if (!syms.length || !page) return;`` —
so the block appeared only when the ledger ALSO had an unregistered symbol, i.e. never on the
demo. The guard of the day (``test_unapplied_actions_have_an_entry.py``) asserted that the call
STRING sat inside ``renderUnregisteredBanner``, which is exactly where the bug was: a string
guard cannot see control flow.

This file RUNS the shipped code instead. Node (Playwright's driver, the interpreter
``test_web_js_parses.py`` uses) evaluates the real head of ``web/app.js`` — everything up to the
KPI band, i.e. ``renderHeader`` and both banners, verbatim — together with the real
``web/format.js`` and ``web/names.js``, against a minimal DOM stub, then calls
``renderHeader()`` exactly as ``boot()`` does and reports what ended up in the page.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

_WEB = Path(__file__).resolve().parents[2] / "web"

#: Where the header section of app.js ends. The slice is app.js's own IIFE up to here, so the
#: helpers (`$`, `el`, `acctZh`, `f`) and the render functions are the shipped ones.
_HEAD_END = "/* ============ B. KPI band"


def _node() -> Path | None:
    import playwright

    for name in ("node.exe", "node"):
        node = Path(playwright.__file__).parent / "driver" / name
        if node.exists():
            return node
    return None


# A DOM just big enough for renderHeader: element creation, a tree, id / class lookup,
# insertBefore / remove. Anything the header code touches that is missing here throws, and a
# throw fails the test loudly (non-zero exit) rather than passing vacuously.
_DOM = r"""
class Node {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase(); this.children = []; this.parentNode = null;
    this.id = ''; this.className = ''; this._text = ''; this.attrs = {}; this.title = '';
    this.href = ''; this.innerHTML = '';
  }
  get firstChild() { return this.children[0] || null; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const sib = this.parentNode.children; const i = sib.indexOf(this);
    return sib[i + 1] || null;
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  appendChild(n) { return this.insertBefore(n, null); }
  insertBefore(n, ref) {
    if (n.parentNode) n.remove();
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(n); else this.children.splice(i, 0, n);
    n.parentNode = this; return n;
  }
  remove() {
    if (!this.parentNode) return;
    const sib = this.parentNode.children; sib.splice(sib.indexOf(this), 1);
    this.parentNode = null;
  }
  removeAttribute(k) { delete this.attrs[k]; if (k === 'href') this.href = ''; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  *walk() { yield this; for (const c of this.children) yield* c.walk(); }
}
const root = new Node('body');
const page = new Node('div'); page.className = 'page'; root.appendChild(page);
['asof-value', 'report-ccy', 'fresh-chip'].forEach((id) => {
  const n = new Node('span'); n.id = id; page.appendChild(n);
});
const match = (n, sel) => sel[0] === '#' ? n.id === sel.slice(1)
  : sel[0] === '.' ? String(n.className).split(/\s+/).indexOf(sel.slice(1)) !== -1 : false;
const document = {
  createElement: (t) => new Node(t),
  querySelector: (sel) => {
    for (const n of root.walk()) if (match(n, sel)) return n;
    return null;
  },
  getElementById: (id) => {
    for (const n of root.walk()) if (n.id === id) return n;
    return null;
  },
};
const window = {};
"""

_REPORT = r"""
const out = {};
const bars = page.children.filter((c) => c.id === 'unreg-banner' || c.id === 'unapplied-banner');
out.order = bars.map((c) => c.id);
const ua = document.getElementById('unapplied-banner');
if (ua) {
  const links = [...ua.walk()].filter((n) => n.tagName === 'A');
  out.unapplied = {
    text: ua.textContent,
    links: links.map((a) => ({ href: a.href, text: a.textContent })),
  };
}
return out;
"""


def _render(payload: dict[str, Any]) -> dict[str, Any]:
    node = _node()
    if node is None:
        pytest.skip("no Node interpreter (Playwright driver) in this venv")
    app = (_WEB / "app.js").read_text(encoding="utf-8")
    head = app[:app.index(_HEAD_END)]
    code = (
        _DOM
        + "(function () {\n" + (_WEB / "format.js").read_text(encoding="utf-8") + "\n})();\n"
        + "(function () {\n" + (_WEB / "names.js").read_text(encoding="utf-8") + "\n})();\n"
        # app.js's own IIFE, opened by its first line and closed here after the header.
        + "const result = " + head
        + "  D = " + json.dumps(payload, ensure_ascii=False) + ";\n"
        + "  renderHeader();\n"
        + _REPORT
        + "})();\n"
        + "process.stdout.write(JSON.stringify(result));\n"
    )
    # Over stdin, not `-e`: the three files exceed Windows' 32K command-line limit.
    done = subprocess.run([str(node), "-"], input=code, capture_output=True, timeout=30,
                          encoding="utf-8", check=False)
    assert done.returncode == 0, done.stderr
    result: dict[str, Any] = json.loads(done.stdout)
    return result


_UNAPPLIED = {
    "action_id": 7, "account_id": "tw_broker", "symbol": "2882", "from_symbol": "2882",
    "to_symbol": "2882", "date": "2026-09-23", "kind": "SPLIT", "kind_label": "拆併股",
    "reason": "2882（台灣券商）於 2026-09-23 沒有持倉，無法套用",
}


def _payload(*, unregistered: list[str], unapplied: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "as_of": "2026-09-24T10:00:00+08:00", "reporting_currency": "TWD",
        "freshness": {"any_stale": False, "prices": [], "fx": [],
                      "unregistered_symbols": unregistered},
        "unapplied_actions": unapplied,
    }


def test_the_block_renders_on_a_ledger_with_no_unregistered_symbol() -> None:
    """The demo's shape, and the bounce: ZERO unregistered symbols, ONE unapplied action."""
    got = _render(_payload(unregistered=[], unapplied=[_UNAPPLIED]))
    assert got["order"] == ["unapplied-banner"], (
        "#unapplied-banner missing — the unapplied block is gated by another banner's "
        f"early return: {got}")
    (link,) = got["unapplied"]["links"]
    assert link["href"].startswith("trades.html?ledger=action&action_id=7&")
    assert "account_id=tw_broker" in link["href"] and "symbol=2882" in link["href"]
    assert link["text"] == "前往該筆公司行動"
    text = got["unapplied"]["text"]
    assert "1 筆公司行動無法套用" in text and "台灣券商" in text and "拆併股" in text
    assert "tw_broker" not in text   # the account through pdNames, never the raw id


def test_both_blocks_render_independently_and_in_order() -> None:
    got = _render(_payload(unregistered=["9999"], unapplied=[_UNAPPLIED]))
    assert got["order"] == ["unreg-banner", "unapplied-banner"]


def test_each_block_is_absent_when_its_own_list_is_empty() -> None:
    assert _render(_payload(unregistered=[], unapplied=[]))["order"] == []
    assert _render(_payload(unregistered=["9999"], unapplied=[]))["order"] == ["unreg-banner"]
