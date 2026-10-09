"""DEF-085: every gate the dry run marks red or amber says what to do about it.

Owner ruling A (2026-09-29): H-05 expects 「對應的 R1–R8 顯示 ✗ 或 ⓘ 並給出可執行的修正指引」;
the form is the developer's to choose. Root cause (3a35454): ``api/insight_service.py:1725-
1731`` ``_RULE_FIX`` deliberately left R6 out (「a top-up is not in the §7.2 enum」) and left
R1 out without a word, and ``_g7`` sent the master-unset warning with ``fix: None`` — while
``web/pipeline.js`` already defined 「前往額度設定」 (``fund_quota``) and 「前往 AI 大師設定」
(``activate_role``) for exactly those rows. No backend path ever emitted either kind.
``web/pipeline-preflight.js:33`` draws a button only for a fix kind, so the rows had none.

Why no test caught it: ``test_pipeline_preflight_api.py`` PINNED ``r6.fix is None`` as the
design, and ``test_def031_universe_edit.py::test_every_backend_fix_kind_has_a_real_action``
checked one direction only (every kind the backend sends has a frontend action). The reverse
— a frontend action no backend path sends — is what a dead 「前往額度設定」 looks like, and
nothing looked. Both directions are checked below, plus the rule itself: no failing or
warning gate without a fix.

The amount (「剩餘 $-0.01000」 beside the node's 「餘 $-0.01」) comes from two sites printing
the raw Decimal; both now go through ``shared.money.usd_display``, the twin of the
frontend's ``'$' + fmt.num(v, 2)``.
"""

import re
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import insight_service as svc
from portfolio_dash.llm_insight.gating import GateFinding, GateResult
from portfolio_dash.shared.llm_config import add_topup
from portfolio_dash.shared.money import usd_display

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = (_ROOT / "portfolio_dash" / "api" / "insight_service.py").read_text(encoding="utf-8")
_JS = (_ROOT / "web" / "pipeline.js").read_text(encoding="utf-8")

#: Frontend fix kinds no gate needs to send, each with its reason.
_FRONTEND_ONLY = {
    # An alias label for the create_schedule action: G1 reports an unbound task as 未排程
    # and sends create_schedule, so no gate state calls for it. It has a real action (the
    # schedule dialog), so it can never render as a dead button.
    "enable_schedule",
}


def _frontend_kinds() -> set[str]:
    block = re.search(r"var FIX_KINDS = \{(.*?)\n  \};", _JS, re.S)
    assert block is not None
    return set(re.findall(r"^\s*(\w+):\s*\{", block.group(1), re.M))


def _backend_kinds() -> set[str]:
    kinds = set(re.findall(r'"fix":\s*\{\s*"kind":\s*"(\w+)"', _BACKEND))
    rule_fix = re.search(r"_RULE_FIX[^=]*=\s*\{([^}]*)\}", _BACKEND)
    assert rule_fix is not None
    return kinds | set(re.findall(r':\s*"(\w+)"', rule_fix.group(1)))


def test_every_frontend_fix_kind_is_sent_by_some_gate() -> None:
    """The reverse of DEF-031's check: a frontend action nobody sends is a dead fix."""
    frontend, backend = _frontend_kinds(), _backend_kinds()
    assert len(frontend) >= 9 and "edit_universe" in backend   # guard both parses
    assert frontend - backend - _FRONTEND_ONLY == set()
    assert _FRONTEND_ONLY <= frontend, "a whitelisted kind left the frontend — drop it here"


def _result(*findings: tuple[str, str]) -> GateResult:
    return GateResult(
        verdict="blocked",
        gates=[GateFinding(id=i, lv=lv, msg=f"{i} {lv}") for i, lv in findings],  # type: ignore[arg-type]
        target_symbols=[None], data_anomaly_symbols=[],
    )


@pytest.mark.parametrize("rule_id, lv", [
    ("R1", "block"), ("R2", "block"), ("R2", "info"), ("R3", "block"), ("R4", "warn"),
    ("R5", "info"), ("R6", "block"),
])
def test_every_rule_slot_that_does_not_pass_has_a_fix(rule_id: str, lv: str) -> None:
    gates = svc._rule_gates(_result((rule_id, lv)), disabled_template_id=None, stale_prices=[])
    gate = next(g for g in gates if g["id"] == rule_id)
    assert gate["lv"] != "ok"
    assert gate["fix"] is not None and gate["fix"]["kind"] in _frontend_kinds(), gate


def test_the_quota_row_leads_to_the_quota_page() -> None:
    gates = svc._rule_gates(_result(("R6", "block")), disabled_template_id=None,
                            stale_prices=[])
    r6 = next(g for g in gates if g["id"] == "R6")
    assert r6["fix"] == {"kind": "fund_quota"}
    assert re.search(r"fund_quota:\s*\{\s*label:\s*'前往額度設定',\s*run:\s*function \(\)\s*"
                     r"\{\s*go\('settings\.html#llm'\);", _JS)


def test_the_master_unset_warning_leads_to_the_master_setting(
    golden_db: sqlite3.Connection,
) -> None:
    g7 = svc._g7(golden_db, self_correct=True, master_configured=False,
                 unapplied_calibration=False)
    assert g7["lv"] == "warn" and g7["fix"] == {"kind": "activate_role"}


@pytest.mark.parametrize("value, shown", [
    ("-0.01", "$-0.01"), ("-0.0100000", "$-0.01"), ("0", "$0.00"), ("-0.004", "$0.00"),
    ("3.8014615", "$3.80"), ("0.125", "$0.13"), ("0.135", "$0.14"), ("1234.5", "$1,234.50"),
])
def test_usd_display_is_the_frontends_fmt_num_2(value: str, shown: str) -> None:
    """``web/format.js`` ``num(v, 2)``: digit-string HALF-UP, thousands separators, and a
    negative that rounds away reads as zero. ``0.125``/``0.135`` are the HALF_EVEN traps
    the node's old ``quantize`` fell into (it printed ``$0.12``)."""
    assert usd_display(Decimal(value)) == shown


def test_the_gate_and_the_node_print_one_balance_one_way(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """H-05's setting: remaining quota −$0.01. R6 and the pipeline exec node agree."""
    add_topup(golden_db, Decimal("0.02"))
    golden_db.execute(
        "INSERT INTO llm_usage (ts, model, agent, input_tokens, output_tokens, cost) "
        "VALUES ('2026-06-11T00:00:00', 'm', 't', 1, 1, '0.03000')")
    golden_db.commit()
    sp = api_client.post("/api/strategy-prompts",
                         json={"name": "S", "body": "{{kpis_json}}"}).json()
    it = api_client.post("/api/insight-types", json={
        "name": "Q", "scope": "portfolio", "strategy_ids": [sp["id"]]}).json()

    body = api_client.post(f"/api/insight-tasks/{it['id']}/preflight").json()
    r6 = next(g for g in body["gates"] if g["id"] == "R6")
    assert r6["msg"] == "額度耗盡（剩餘 $-0.01）"
    assert r6["fix"] == {"kind": "fund_quota"}

    diag = api_client.get(f"/api/insight-tasks/{it['id']}/diagnose").json()
    assert next(g for g in diag["gates"] if g["id"] == "R6")["fix"] == {"kind": "fund_quota"}

    status = api_client.get("/api/insight-tasks/status").json()
    node = next(t for t in status["tasks"] if t["id"] == it["id"])["nodes"]["exec"]
    assert node["sub"] == "餘 $-0.01"


def test_the_budget_refusal_prints_the_same_amount(golden_db: sqlite3.Connection) -> None:
    """The AI door's 402 (F-07's toast) is the third server sentence with this balance."""
    from portfolio_dash.shared.llm_config import LLMBudgetExceeded, check_budget

    add_topup(golden_db, Decimal("0.01"))
    golden_db.execute(
        "INSERT INTO llm_usage (ts, model, agent, input_tokens, output_tokens, cost) "
        "VALUES ('2026-06-11T00:00:00', 'm', 't', 1, 1, '0.0418601')")
    with pytest.raises(LLMBudgetExceeded) as exc:
        check_budget(golden_db)
    assert str(exc.value) == "AI 額度用盡（剩餘 $-0.03）— 補充額度後即可繼續使用"
