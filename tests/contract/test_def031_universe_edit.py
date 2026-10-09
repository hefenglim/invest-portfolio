"""DEF-031 (2026-09-23): the insight-task drawer's 「編輯標的」 must actually edit the universe.

The button's whole handler was
``window.toast('編輯標的', 'ok', '沿用既有標的選擇器（持倉＋觀察清單）')`` (pipeline.js) — a
green check-mark, no dialog, no request — and the dry-run preflight's R2/R4 fix action
(``edit_universe``) routed to the same dead end via ``openDrawer(t, 'input')``. The backend
never lacked the endpoint: ``PUT /api/insight-tasks/{id}`` has carried ``universe`` since the
composer shipped. What was missing was the frontend, so this file pins BOTH halves:

* the API contract the new dialog writes through (round trip + the input node's count), and
  the read-side de-duplication that makes a stored ``["AAPL", "AAPL"]`` count as ONE symbol;
* the static wiring — the button and the fix action open ``ppUniverseModal``, and no click
  handler in the pipeline pages is a toast and nothing else (the class of this defect).
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parents[2]
_WEB = _ROOT / "web"


def _per_symbol_task(api_client: TestClient) -> int:
    sp = api_client.post(
        "/api/strategy-prompts", json={"name": "S", "body": "{{kpis_json}}"}
    ).json()
    it = api_client.post(
        "/api/insight-tasks",
        json={"name": "個股健檢", "scope": "per_symbol", "strategy_ids": [sp["id"]]},
    ).json()
    return int(it["id"])


def _full(api_client: TestClient, tid: int) -> dict[str, object]:
    return next(t for t in api_client.get("/api/insight-tasks").json() if t["id"] == tid)


def _input_text(api_client: TestClient, tid: int) -> str:
    status = api_client.get("/api/insight-tasks/status").json()
    task = next(t for t in status["tasks"] if t["id"] == tid)
    return str(task["nodes"]["input"]["text"])


def _put_universe(api_client: TestClient, tid: int, universe: object) -> None:
    full = _full(api_client, tid)
    body = {
        "name": full["name"], "scope": full["scope"],
        "strategy_ids": [s["id"] for s in full["strategies"]],  # type: ignore[attr-defined]
        "use_system_prompt": full["use_system_prompt"], "self_correct": full["self_correct"],
        "universe": universe, "alert_rules": full["alert_rules"], "enabled": full["enabled"],
        "horizon_days": full["horizon_days"], "eval_prompt": full["eval_prompt"],
    }
    resp = api_client.put(f"/api/insight-tasks/{tid}", json=body)
    assert resp.status_code == 200, resp.text


def test_the_dialog_s_write_round_trips_and_the_input_node_counts_it(
    api_client: TestClient,
) -> None:
    tid = _per_symbol_task(api_client)
    assert _input_text(api_client, tid) == "2 檔標的"          # golden DB: 2330 + AAPL held
    _put_universe(api_client, tid, {"mode": "custom", "symbols": ["AAPL"]})
    assert _full(api_client, tid)["universe"] == {"mode": "custom", "symbols": ["AAPL"]}
    assert _input_text(api_client, tid) == "1 檔標的"
    _put_universe(api_client, tid, {"mode": "all"})
    assert _input_text(api_client, tid) == "2 檔標的"
    # a PUT that only changes the universe must not reset the rest of the row
    assert _full(api_client, tid)["enabled"] is True


def test_a_duplicated_custom_symbol_is_one_symbol(api_client: TestClient) -> None:
    """DEF-032's stored consequence: the wizard's two AAPL boxes wrote ["AAPL", "AAPL"]."""
    tid = _per_symbol_task(api_client)
    _put_universe(api_client, tid, {"mode": "custom", "symbols": ["AAPL", "AAPL", "2330"]})
    assert _input_text(api_client, tid) == "2 檔標的"


def test_the_button_and_the_fix_action_open_the_universe_dialog() -> None:
    src = (_WEB / "pipeline.js").read_text(encoding="utf-8")
    assert "沿用既有標的選擇器" not in src, "the toast-only 編輯標的 handler is back"
    fix = re.search(r"edit_universe:\s*\{[^}]*run:\s*function\s*\(t\)\s*\{([^}]*)\}", src)
    assert fix is not None and "ppUniverseModal" in fix.group(1), (
        "the preflight's edit_universe fix must open the universe dialog directly")
    button = re.search(r"'編輯標的'\);\s*\n\s*be\.type = 'button';\s*\n\s*"
                       r"be\.addEventListener\('click', function \(\) \{([^}]*)\}", src)
    assert button is not None and "ppUniverseModal" in button.group(1), (
        "the drawer's 編輯標的 button must open the universe dialog")


#: DEF-031 class scan: a click handler whose ENTIRE body is one toast call is a button that
#: does nothing but claim it did. Pipeline pages only — the verifier's scope.
_TOAST_ONLY = re.compile(
    r"addEventListener\(\s*'click'\s*,\s*(?:function\s*\([^)]*\)|\([^)]*\)\s*=>)\s*\{\s*"
    r"(?:window\.)?toast\([^;]*\);\s*\}\s*\)"
)


def test_no_pipeline_click_handler_is_only_a_toast() -> None:
    offenders = []
    for name in ("pipeline.js", "pipeline-wizard.js", "pipeline-preflight.js",
                 "pipeline-hub.html"):
        text = (_WEB / name).read_text(encoding="utf-8")
        offenders += [f"{name}:{text.count(chr(10), 0, m.start()) + 1}"
                      for m in _TOAST_ONLY.finditer(text)]
    assert not offenders, f"click handlers that only toast: {offenders}"


def test_every_backend_fix_kind_has_a_real_action() -> None:
    """pipeline-preflight.js falls back to 「請於對應頁面完成設定」 for an UNMAPPED fix kind —
    a toast-only button. Every kind the backend can emit must therefore be in FIX_KINDS."""
    backend = (_ROOT / "portfolio_dash" / "api" / "insight_service.py").read_text(
        encoding="utf-8")
    kinds = set(re.findall(r'"fix":\s*\{\s*"kind":\s*"(\w+)"', backend))
    rule_fix = re.search(r"_RULE_FIX[^=]*=\s*\{([^}]*)\}", backend)
    assert rule_fix is not None
    kinds |= set(re.findall(r':\s*"(\w+)"', rule_fix.group(1)))
    assert "edit_universe" in kinds and len(kinds) >= 5, kinds   # guard the parse itself
    js = (_WEB / "pipeline.js").read_text(encoding="utf-8")
    block = re.search(r"var FIX_KINDS = \{(.*?)\n  \};", js, re.S)
    assert block is not None
    mapped = set(re.findall(r"^\s*(\w+):\s*\{", block.group(1), re.M))
    assert kinds <= mapped, f"backend fix kinds with no frontend action: {kinds - mapped}"
