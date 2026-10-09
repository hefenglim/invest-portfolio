"""DEF-032 / DEF-031 (2026-09-23): the insight-task universe counts SYMBOLS, not holding rows.

新增洞察任務精靈 built its universe with
``REF.held = holdings.map(function (h) { return h.symbol; })`` (pipeline-wizard.js). The
dashboard's holdings are keyed by (帳戶, 標的), so a symbol held in two accounts is two rows:
AAPL in 嘉信 and Moomoo showed TWO checkboxes, and 「全部持倉 14 檔・14 張卡・~$0.14」 was
printed for a book the backend resolves to 13 (``insight_service._resolve_universe`` takes
``sorted({h.symbol for h in data.holdings})``). Ticking both AAPL boxes also stored
``["AAPL", "AAPL"]`` as a custom universe — two cards for one symbol.

The fix is ONE definition, ``window.ppUniverseSource`` in pipeline.js, read by the wizard AND
the drawer's 編輯標的 dialog (DEF-031). This test drives the REAL pipeline.js under node with
the golden dashboard payload plus a second AAPL row, and pins the symbol count, the account
chips and the cost estimate's input; the static half pins that neither caller re-derives it.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_WEB = _ROOT / "web"
_GOLDEN = _ROOT / "tests" / "golden" / "dashboard_full.json"

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const input = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const node = () => ({ className: '', dataset: {}, style: {}, appendChild() {},
                      addEventListener() {}, classList: { add() {}, remove() {} } });
const sandbox = {
  document: { readyState: 'complete', querySelector: () => null,
              querySelectorAll: () => [], createElement: node, addEventListener() {} },
  requestAnimationFrame: (f) => f(),
  console: console,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
const S = sandbox.ppUniverseSource;
const U = sandbox.ppUniverseSymbols;
if (typeof S !== 'function' || typeof U !== 'function') {
  process.stdout.write(JSON.stringify({ missing: true }));
  process.exit(0);
}
const src1 = S(input.dash, input.instruments);
process.stdout.write(JSON.stringify({
  missing: false,
  held: src1.held,
  registered: src1.registered,
  rows: src1.rows,
  all: U({ mode: 'all' }, src1),
  registeredAll: U({ mode: 'all_registered' }, src1),
  custom: U({ mode: 'custom', symbols: ['AAPL', 'AAPL', 'MSFT'] }, src1),
  legacy: U(null, src1),
}));
"""


def _node() -> Path | None:
    import playwright

    node = Path(playwright.__file__).parent / "driver" / "node.exe"
    if node.exists():
        return node
    node = Path(playwright.__file__).parent / "driver" / "node"
    return node if node.exists() else None


@pytest.fixture(scope="module")
def result(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    node = _node()
    if node is None:
        pytest.skip("Playwright's bundled node is not installed in this venv")
    dash = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    aapl = next(h for h in dash["holdings"] if h["symbol"] == "AAPL")
    second = copy.deepcopy(aapl)
    second["account_id"] = "moomoo_my"
    second["account_name"] = "Moomoo MY"
    dash["holdings"].append(second)          # AAPL now held in TWO accounts
    instruments = {"list": [{"symbol": h["symbol"], "name": h["name"], "archived": False}
                            for h in dash["holdings"]]
                   + [{"symbol": "TSLA", "name": "Tesla", "archived": False}]}
    tmp = tmp_path_factory.mktemp("def032")
    (tmp / "h.js").write_text(_HARNESS, encoding="utf-8")
    (tmp / "in.json").write_text(json.dumps({"dash": dash, "instruments": instruments}),
                                 encoding="utf-8")
    proc = subprocess.run(
        [str(node), str(tmp / "h.js"), str(_WEB / "pipeline.js"), str(tmp / "in.json")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}"
    out: dict[str, Any] = json.loads(proc.stdout)
    out["holding_rows"] = len(dash["holdings"])
    return out


def test_the_shared_universe_source_exists(result: dict[str, Any]) -> None:
    assert not result["missing"], (
        "window.ppUniverseSource / ppUniverseSymbols are not defined by pipeline.js — the "
        "wizard and the 編輯標的 dialog have no single definition to share")


def test_a_symbol_held_in_two_accounts_counts_once(result: dict[str, Any]) -> None:
    """The DEF-032 reproduction: 9 holding rows, 8 symbols — the count must say 8."""
    assert result["holding_rows"] == 9
    assert result["held"].count("AAPL") == 1
    assert len(result["held"]) == 8
    assert len(result["all"]) == 8           # 「全部持倉 N 檔」 and the ~$ estimate read this
    aapl = [r for r in result["rows"] if r["symbol"] == "AAPL"]
    assert len(aapl) == 1, "one checkbox per symbol, not per (account, symbol) row"
    assert aapl[0]["accounts"] == ["schwab", "moomoo_my"], "both accounts ride on ONE row"


def test_the_counts_mirror_the_backend_resolver(result: dict[str, Any]) -> None:
    """mode:all = sorted held set; all_registered = sorted registry; custom = de-duplicated."""
    assert result["all"] == sorted(result["all"])
    assert result["registeredAll"] == sorted(set(result["registeredAll"]))
    assert "TSLA" in result["registeredAll"] and "TSLA" not in result["all"]
    assert result["custom"] == ["AAPL", "MSFT"]
    assert result["legacy"] == result["all"], "a task with no stored universe follows holdings"


def test_watch_only_symbols_are_marked_and_carry_no_account(result: dict[str, Any]) -> None:
    tsla = next(r for r in result["rows"] if r["symbol"] == "TSLA")
    assert tsla["held"] is False and tsla["accounts"] == []


def test_the_wizard_reads_the_shared_source_instead_of_holding_rows() -> None:
    """Static half: the old per-row mapping must not come back in either caller."""
    wizard = (_WEB / "pipeline-wizard.js").read_text(encoding="utf-8")
    assert "ppUniverseSource" in wizard
    assert not re.search(r"holdings\)\s*\|\|\s*\[\]\)\.map\(", wizard), (
        "pipeline-wizard.js maps dashboard holding ROWS to symbols again (DEF-032)")
    pipeline = (_WEB / "pipeline.js").read_text(encoding="utf-8")
    assert pipeline.count("function universeSource(") == 1
    assert "ppUniverseSource" in pipeline and "ppUniverseModal" in pipeline
