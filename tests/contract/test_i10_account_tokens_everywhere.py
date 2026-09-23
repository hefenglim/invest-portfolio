"""I-10 (E-1): the last backend sentences and labels that named an account by bare id or by
its English ``accounts.name`` — and the print reports, where no fetch layer used to reach.

``tests/contract/test_account_ref_seam.py`` carried five ``_PENDING`` sites after the DEF-008
wave; this change empties it:

* ``broker_import.py`` / ``input_center.py`` built 「帳戶 {x} 不存在」 themselves — a blank id
  rendered 「帳戶  不存在」 (two spaces, no name) where every other door says 「帳戶不可空白」.
  Both now call ``validate.unknown_account_message`` (the one owner of that sentence);
* ``validate.py`` D31's depth-cap confirm named the account by bare id → ``account_ref``;
* ``cost_basis.py``'s six E-rejections already embedded the token through a local named
  ``acct`` — the scanner cannot tell a token-holding local from an id, so the local is now
  named for what it holds (``acct_ref``); the sentences were already right;
* ``HoldingRow.account_name`` was ``accounts.name`` (「TW Broker」) — the English label the
  owner never chose — and the print reports rendered it server-side. It is now the token,
  like every other backend sentence. The dashboard / drawer / rebalance payloads reach the
  page through ``pdApi``, which resolves it; the print reports are DOWNLOADED as offline
  files, so ``pdApi.download`` resolves tokens in a text blob before it is saved (HTML-escaped
  for ``text/html``, the UTF-8 BOM kept for Excel, binary untouched). Rejected: loading
  ``names.js`` from the report page — the file is opened offline from disk, where a relative
  script cannot load, and inlining a copy of the name table would make a second naming
  authority.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.validate import _depth_cap_issue
from tests.contract.test_account_ref_seam import _PENDING

_WEB = Path(__file__).resolve().parents[2] / "web"


def test_the_pending_list_is_empty() -> None:
    assert _PENDING == {}


def test_a_blank_account_reads_the_shared_sentence_at_both_router_doors(
    api_client: TestClient,
) -> None:
    r = api_client.post("/api/broker/convert", json={
        "broker": "schwab", "account": " ", "exports": [{"name": "a.csv", "text": "x"}]})
    assert r.status_code == 400 and r.json()["error"]["message"] == "帳戶不可空白"
    r = api_client.get("/api/input/holdings", params={"account": " "})
    assert r.status_code == 404 and r.json()["error"]["message"] == "帳戶不可空白"
    r = api_client.get("/api/input/holdings", params={"account": "nope"})
    assert r.json()["error"]["message"] == "帳戶 nope 不存在"


class _CappedIndex:
    def depth_capped_symbols(self) -> frozenset[tuple[str, str]]:
        return frozenset({("tw_broker", "2330")})


def test_the_depth_cap_confirm_names_the_account_by_token() -> None:
    issue = _depth_cap_issue(_CappedIndex(), {"2330"})  # type: ignore[arg-type]
    assert issue is not None
    assert "2330（{account:tw_broker}）" in issue.message


def test_the_dashboard_holding_label_is_the_token(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    rows = api_client.get("/api/dashboard").json()["holdings"]
    assert rows
    for h in rows:
        assert h["account_name"] == "{account:" + h["account_id"] + "}", h


def test_the_holdings_report_carries_tokens_not_english_names(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    r = api_client.post("/api/export/holdings-report", json={})
    assert r.status_code == 200
    doc = r.content.decode("utf-8")
    assert "{account:tw_broker}" in doc
    assert "TW Broker" not in doc


# ------------------------------------------------ the download seam resolves the tokens

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const apiSrc = fs.readFileSync(process.argv[2], 'utf8');
const namesSrc = fs.readFileSync(process.argv[3], 'utf8');
const cases = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));

(async function () {
  const out = {};
  for (const [key, c] of Object.entries(cases)) {
    let saved = null;
    const sb = {
      window: { location: { pathname: '/index.html', replace: function () {} } },
      document: { dispatchEvent: function () {},
                  createElement: function () {
                    return { click: function () {}, remove: function () {} }; },
                  body: { appendChild: function () {} } },
      CustomEvent: function () {}, AbortController: function () { this.signal = {}; },
      URLSearchParams: URLSearchParams, setTimeout: function () {}, console: console,
      Blob: Blob, TextDecoder: TextDecoder,
      URL: { createObjectURL: function (b) { saved = b; return 'blob:x'; },
             revokeObjectURL: function () {} },
    };
    sb.globalThis = sb;
    vm.createContext(sb);
    vm.runInContext(apiSrc, sb);
    vm.runInContext(namesSrc, sb);
    const bytes = Buffer.from(c.b64, 'base64');
    sb.fetch = async function () {
      return { ok: true, status: 200,
               headers: { get: function (h) {
                 return h.toLowerCase() === 'content-type'
                   ? c.type : 'attachment; filename="r"'; } },
               blob: async function () { return new Blob([bytes], { type: c.type }); } };
    };
    await sb.window.pdApi.download('/api/export/x', {});
    const buf = Buffer.from(await saved.arrayBuffer());
    out[key] = { b64: buf.toString('base64'), same: buf.equals(bytes) };
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _node() -> Path | None:
    import playwright

    for name in ("node.exe", "node"):
        node = Path(playwright.__file__).parent / "driver" / name
        if node.exists():
            return node
    return None


def _download(tmp_path: Path, cases: dict[str, tuple[str, bytes]]) -> dict[str, Any]:
    import base64

    node = _node()
    if node is None:
        pytest.skip("Playwright's bundled node is not installed in this venv")
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    data = tmp_path / "cases.json"
    data.write_text(json.dumps({k: {"type": t, "b64": base64.b64encode(b).decode()}
                                for k, (t, b) in cases.items()}), encoding="utf-8")
    proc = subprocess.run(
        [str(node), str(harness), str(_WEB / "api.js"), str(_WEB / "names.js"), str(data)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    out: dict[str, Any] = json.loads(proc.stdout)
    for v in out.values():
        v["bytes"] = base64.b64decode(v["b64"])
    return out


def test_a_downloaded_report_is_saved_with_display_names(tmp_path: Path) -> None:
    bom = b"\xef\xbb\xbf"
    got = _download(tmp_path, {
        "html": ("text/html; charset=utf-8",
                 b'<td class="l">{account:tw_broker}</td><td>{account:a<b}</td>'),
        "csv": ("text/csv; charset=utf-8", bom + "帳戶\n{account:schwab}\n".encode()),
        "plain_html": ("text/html; charset=utf-8", b"<p>no tokens here</p>"),
        "zip": ("application/zip", b"PK\x03\x04{account:tw_broker}"),
    })
    assert got["html"]["bytes"].decode() == (
        '<td class="l">台灣券商</td><td>a&lt;b</td>')            # escaped for HTML
    assert got["csv"]["bytes"] == bom + "帳戶\n嘉信 Schwab\n".encode()   # BOM kept
    assert got["plain_html"]["same"] is True                   # no token: byte-identical
    assert got["zip"]["same"] is True                          # binary: never touched
