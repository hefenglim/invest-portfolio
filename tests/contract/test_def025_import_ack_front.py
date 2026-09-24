"""DEF-025 (owner ruling 2026-09-24): the ONE per-row acknowledgement flow, RUN — not grepped.

``web/import-ack.js`` is what the three bulk doors (券商對帳單, CSV 匯入, AI 輸入) now share. This
file executes the shipped file in Node (Playwright's driver) against a minimal DOM stub and a
scripted ``pdApi``, drives its dialog the way the owner would — tick a box, press a button —
and asserts the REQUESTS it sends. It replaces the DEF-027 guard that only scanned
``broker-import.js`` for a literal ``ack_warnings: true`` (R2 bounced exactly that class of
guard: a string that exists is not a behaviour that happens).

Pinned: the first commit is unacknowledged; a 422 opens a dialog listing every warning row
among those being written, each UNTICKED, a 賣超 named with 「成本基礎會被永久捨棄」; only the
ticked rows are re-sent (``select``) and named (``ack_rows``); 略過 sends none of them and no
``ack_rows``; 取消 sends nothing more.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

_WEB = Path(__file__).resolve().parents[2] / "web"
_SRC = (_WEB / "import-ack.js").read_text(encoding="utf-8")


def _node() -> Path | None:
    import playwright

    for name in ("node.exe", "node"):
        node = Path(playwright.__file__).parent / "driver" / name
        if node.exists():
            return node
    return None


_HARNESS = r"""
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this._t = ''; this.style = {};
    this.dataset = {}; this.className = ''; this.type = ''; this.checked = false;
    this.disabled = false; this.parent = null; this.handlers = {}; }
  set textContent(v) { this._t = String(v); this.children = []; }
  get textContent() { return this._t + this.children.map((c) => c.textContent).join(''); }
  appendChild(n) { n.parent = this; this.children.push(n); return n; }
  addEventListener(t, fn) { (this.handlers[t] = this.handlers[t] || []).push(fn); }
  fire(t) { (this.handlers[t] || []).forEach((fn) => fn({ target: this })); }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this);
  }
  *walk() { yield this; for (const c of this.children) yield* c.walk(); }
}
const body = new Node('body');
globalThis.document = { createElement: (t) => new Node(t), body: body };
globalThis.window = globalThis;
const calls = [];
let queue = [];
window.pdApi = { post: async (path, b) => {
  calls.push({ path: path, body: JSON.parse(JSON.stringify(b)) });
  const next = queue.shift();
  if (!next) throw new Error('unscripted call ' + path);
  if (next.err) { const e = new Error(next.err.message || ''); e.status = next.err.status;
                  e.code = next.err.code; throw e; }
  return next.ok;
} };
"""

_DRIVER = r"""
const SC = JSON.parse(process.argv[process.argv.length - 1]);
const tick = () => new Promise((r) => setTimeout(r, 0));
(async () => {
  queue = SC.queue;
  let result, error = null;
  const p = window.pdImportAck.commit(SC.opts).then((r) => { result = r; },
                                                    (e) => { error = { code: e.code }; });
  let dialog = null;
  for (let i = 0; i < 50 && !dialog && result === undefined && !error; i++) {
    await tick();
    dialog = body.children.find((c) => c.className === 'modal-backdrop') || null;
  }
  const seen = {};
  if (dialog) {
    const all = Array.from(dialog.walk());
    const boxes = all.filter((n) => n.tag === 'input' && n.type === 'checkbox');
    const btn = (label) => all.find((n) => n.tag === 'button' && n.textContent === label);
    const ok = btn('寫入勾選的警告列');
    seen.text = dialog.textContent;
    seen.ticks = boxes.map((b) => ({ n: Number(b.dataset.n), checked: b.checked }));
    seen.okDisabledBefore = ok.disabled;
    (SC.tick || []).forEach((n) => {
      const b = boxes.find((x) => Number(x.dataset.n) === n);
      b.checked = true; b.fire('change');
    });
    seen.okDisabledAfter = ok.disabled;
    btn(SC.press).fire('click');
    seen.closed = !body.children.includes(dialog);
  }
  await p;
  process.stdout.write(JSON.stringify({ calls: calls, dialog: dialog ? seen : null,
                                        result: result === undefined ? null : result,
                                        error: error }));
})();
"""

_PREVIEW = {"rows": [
    {"n": 0, "status": "ok", "reason": None, "kinds": [],
     "data": {"symbol": "2330", "trade_date": "2026-06-05", "side": "buy", "quantity": "10"}},
    {"n": 1, "status": "warn", "reason": "賣出 150 股，超過持有的 100 股",
     "kinds": ["sell_exceeds_holdings"],
     "data": {"symbol": "2884", "trade_date": "2026-06-05", "side": "sell", "quantity": "150"}},
]}
_UNACKED: dict[str, Any] = {"err": {"status": 422, "code": "warnings_unacknowledged"}}
_BODY: dict[str, Any] = {"kind": "transactions", "csv_text": "x", "select": [0, 1],
                         "source_name": "貼上 CSV"}
_OPTS: dict[str, Any] = {"body": _BODY, "title": "交易"}


def _run(scenario: dict[str, Any]) -> dict[str, Any]:
    node = _node()
    if node is None:
        pytest.skip("no Node interpreter (Playwright driver) in this venv")
    script = _HARNESS + _SRC + _DRIVER
    proc = subprocess.run([str(node), "-e", script, json.dumps(scenario)],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    out: dict[str, Any] = json.loads(proc.stdout)
    return out


def test_ticking_the_oversell_row_sends_it_in_select_and_in_ack_rows() -> None:
    out = _run({"opts": _OPTS, "tick": [1], "press": "寫入勾選的警告列",
                "queue": [_UNACKED, {"ok": _PREVIEW}, {"ok": {"written": 2, "skipped": 0}}]})
    first, preview, second = out["calls"]
    assert first["path"] == "/api/import/commit"
    assert first["body"]["ack_warnings"] is False and "ack_rows" not in first["body"]
    assert preview == {"path": "/api/import/preview",
                       "body": {"kind": "transactions", "csv_text": "x"}}
    dialog = out["dialog"]
    assert dialog["ticks"] == [{"n": 1, "checked": False}]      # warning rows only, UNTICKED
    assert "第 2 列 2884" in dialog["text"] and "成本基礎會被永久捨棄" in dialog["text"]
    assert "賣出 150 股，超過持有的 100 股" in dialog["text"]       # the server's own sentence
    assert dialog["okDisabledBefore"] is True and dialog["okDisabledAfter"] is False
    assert dialog["closed"] is True
    assert second["body"]["ack_warnings"] is True
    assert second["body"]["select"] == [0, 1] and second["body"]["ack_rows"] == [1]
    assert second["body"]["source_name"] == "貼上 CSV"
    assert out["result"] == {"written": 2, "skipped": 0}


def test_skip_writes_only_the_clean_rows_and_acknowledges_nothing() -> None:
    out = _run({"opts": _OPTS, "press": "略過所有警告列，只寫入其他列",
                "queue": [_UNACKED, {"ok": _PREVIEW}, {"ok": {"written": 1, "skipped": 1}}]})
    second = out["calls"][2]["body"]
    assert second["select"] == [0] and "ack_rows" not in second


def test_cancel_sends_nothing_more() -> None:
    out = _run({"opts": _OPTS, "press": "取消，停在這一步",
                "queue": [_UNACKED, {"ok": _PREVIEW}]})
    assert [c["path"] for c in out["calls"]] == ["/api/import/commit", "/api/import/preview"]
    assert out["result"] == {"cancelled": True}


def test_the_servers_oversell_refusal_opens_the_same_dialog() -> None:
    """A page that sent the file-level ack without ``ack_rows`` gets the server's new code —
    the flow treats it exactly like the first refusal."""
    out = _run({"opts": _OPTS, "tick": [1], "press": "寫入勾選的警告列",
                "queue": [{"err": {"status": 422, "code": "oversell_rows_unacknowledged"}},
                          {"ok": _PREVIEW}, {"ok": {"written": 2}}]})
    assert out["dialog"] is not None and out["calls"][2]["body"]["ack_rows"] == [1]


def test_a_deselected_warning_row_is_never_asked_about() -> None:
    """The ruling's default: the unticked 賣超 row is not in ``select`` — no dialog, and the
    file-level gate is released for the rows that ARE being written."""
    opts = {**_OPTS, "body": {**_BODY, "select": [0]}}
    out = _run({"opts": opts,
                "queue": [_UNACKED, {"ok": _PREVIEW}, {"ok": {"written": 1, "skipped": 1}}]})
    assert out["dialog"] is None
    second = out["calls"][2]["body"]
    assert second["ack_warnings"] is True and second["select"] == [0]
    assert "ack_rows" not in second


def test_a_refusal_after_the_acknowledgement_is_reported_not_looped() -> None:
    out = _run({"opts": _OPTS, "tick": [1], "press": "寫入勾選的警告列",
                "queue": [_UNACKED, {"ok": _PREVIEW},
                          {"err": {"status": 422, "code": "oversell_rows_unacknowledged"}}]})
    assert len(out["calls"]) == 3
    assert out["error"] == {"code": "oversell_rows_unacknowledged"}
