"""DEF-062 — every alert rule has ONE zh name, carried on the wire, and no surface shows its id.

Measured on the demo (1ee7771): 洞察管線 › ＋新增洞察任務 → 觸發「預警觸發」→ 監聽規則 showed 8
of its 15 checkboxes as raw ids (``missing_price``, ``drawdown_from_peak``, ``vol_spike``,
``rebalance_drift``, ``consensus_change``, ``portfolio_drawdown``, ``currency_weight``,
``target_cross``). ``web/pipeline-wizard.js`` kept its own 7-entry name table and fell back to
the id; ``web/settings-alerts.js`` and ``ops/notify.py`` held two more copies that disagreed in
wording (「價格過期/缺價」 vs 「價格過期」, 「即將除息」 vs 「即將除息提醒」).

Nothing caught it because the one existing drift guard (``tests/unit/test_notify.py``) compared
the PUSH catalog with the registry, and neither frontend table had a guard at all.

**The parent set is the REGISTRY, never the name table.** Every test below iterates
``strategy.rules_config.RULE_IDS`` (or the event ids the code records into ``alert_events``),
so a rule added without a name FAILS — iterating the name table would pass it silently.
"""

import ast
import re
from pathlib import Path

from fastapi.testclient import TestClient

from portfolio_dash.strategy import signal_states
from portfolio_dash.strategy.rules_config import RULE_IDS

_ROOT = Path(__file__).resolve().parents[2]
_PKG = _ROOT / "portfolio_dash"
#: What the verifier looked for on the screen: a snake_case identifier.
_IDENT = re.compile(r"[a-z]+_[a-z_]+")
_CJK = re.compile(r"[一-鿿]")
_UNNAMED = "未命名規則"

#: The owner's wording (ruling 3 of the DEF-062 spec): the settings › 預警規則 page as it read
#: on 1ee7771. Every other surface was brought to these words.
_OWNER_WORDING = {
    "single_weight": "單一標的集中度",
    "sector_weight": "產業集中度",
    "stale_price": "價格過期",
    "missing_price": "缺價",
    "fx_drift": "匯率漂移",
    "exdiv_upcoming": "即將除息提醒",
    "quota_low": "AI 額度偏低",
    "calib_gap": "AI 校準誤差",
    "drawdown_from_peak": "高點回撤",
    "vol_spike": "波動突升",
    "rebalance_drift": "配置漂移",
    "consensus_change": "分析師共識轉弱",
    "target_cross": "目標價穿越",
    "portfolio_drawdown": "組合整體回撤",
    "currency_weight": "幣別集中度",
}


def _bad_name(rid: str, name: object) -> str | None:
    """Why *name* is not an acceptable zh display name for *rid*, or None when it is."""
    if not isinstance(name, str) or not name.strip():
        return f"{rid}: no name on the wire (got {name!r})"
    if name == _UNNAMED:
        return f"{rid}: 「{_UNNAMED}」 — the name table has no entry for a registered rule"
    if _IDENT.search(name) or rid in name:
        return f"{rid}: the name is an identifier ({name!r})"
    if not _CJK.search(name):
        return f"{rid}: the name has no Chinese ({name!r})"
    return None


def _wire(api_client: TestClient) -> dict[str, dict[str, object]]:
    r = api_client.get("/api/alert-rules")
    assert r.status_code == 200, r.text
    return {row["id"]: row for row in r.json()["rules"]}


def test_every_registered_rule_carries_a_zh_name_on_the_wire(api_client: TestClient) -> None:
    """The load-bearing one: GET /api/alert-rules names every rule the registry knows."""
    rows = _wire(api_client)
    assert list(rows) == list(RULE_IDS), "the wire does not list exactly the registry"
    problems = [p for rid in RULE_IDS if (p := _bad_name(rid, rows[rid].get("name")))]
    assert not problems, "rules without a proper zh name:\n" + "\n".join(problems)


def test_the_names_are_the_owner_wording(api_client: TestClient) -> None:
    """Ruling 3: the settings page's words win everywhere (「價格過期」, 「即將除息提醒」…)."""
    rows = _wire(api_client)
    diffs = {rid: (rows[rid].get("name"), want) for rid, want in _OWNER_WORDING.items()
             if rows[rid].get("name") != want}
    assert not diffs, f"wording drifted from the settings page's (got, want): {diffs}"


def test_put_answers_with_names_and_its_errors_name_the_rule(api_client: TestClient) -> None:
    """The PUT echo feeds the same editor, and its 400s reach the page as a toast."""
    bad = api_client.put("/api/alert-rules", json={"rules": [
        {"id": "single_weight", "enabled": True, "value": "2.0"}]})
    assert bad.status_code == 400
    msg = bad.json()["error"]["message"]
    assert "單一標的集中度" in msg and "single_weight" not in msg, msg
    low = api_client.put("/api/alert-rules", json={"rules": [
        {"id": "vol_spike", "enabled": True, "value": "0.1"}]})
    assert low.status_code == 400
    assert "波動突升" in low.json()["error"]["message"], low.json()
    ok = api_client.put("/api/alert-rules", json={"rules": [
        {"id": "target_cross", "enabled": True, "value": None}]})
    assert ok.status_code == 200
    echoed = {row["id"]: row for row in ok.json()["rules"]}
    problems = [p for rid in RULE_IDS if (p := _bad_name(rid, echoed[rid].get("name")))]
    assert not problems, problems


def test_the_notification_list_speaks_the_same_names(api_client: TestClient) -> None:
    """設定 › 通知中心 lists the same rules as subscriptions — one table, one wording."""
    rows = _wire(api_client)
    catalog = {c["id"]: c["label"] for c in api_client.get("/api/notify/config")
               .json()["rule_catalog"]}
    diffs = {rid: (catalog.get(rid), rows[rid]["name"]) for rid in RULE_IDS
             if catalog.get(rid) != rows[rid]["name"]}
    assert not diffs, f"通知中心 names a rule differently from 預警規則 (notify, rules): {diffs}"


def _recorded_rule_literals() -> set[str]:
    """Every literal ``rule_id=`` the code hands to ``alerts_bridge.record_event[_ex]``.

    Found by walking the AST of every module — the rule ids that reach ``alert_events`` (and
    from there the push, the digest, the run detail and a card chip) without being alert rules.
    """
    out: set[str] = set()
    for path in _PKG.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name not in ("record_event", "record_event_ex"):
                continue
            for kw in node.keywords:
                if kw.arg == "rule_id" and isinstance(kw.value, ast.Constant) \
                        and isinstance(kw.value.value, str):
                    out.add(kw.value.value)
    return out


def test_every_event_id_that_reaches_alert_events_has_a_name() -> None:
    """Signal transitions and ``calibration_regression`` are pushed and digested like rules.

    Before DEF-062 ``calibration_regression`` had no name anywhere, so its push read
    「portfolio-dash · 洞察任務 5 calibration_regression」.
    """
    from portfolio_dash.shared.alert_rule_names import rule_name

    literals = _recorded_rule_literals()
    assert "calibration_regression" in literals, f"the scan found nothing it guards: {literals}"
    signals = {signal_states.EVENT_TREND, signal_states.EVENT_CROSS,
               signal_states.EVENT_MOMENTUM}
    problems = [p for rid in sorted(literals | signals | set(RULE_IDS))
                if (p := _bad_name(rid, rule_name(rid)))]
    assert not problems, problems


def test_the_name_table_names_only_registered_ids() -> None:
    """No orphan: a name for an id nothing registers is a rule the registry forgot."""
    from portfolio_dash.shared.alert_rule_names import ALERT_RULE_NAMES, EVENT_RULE_NAMES

    assert set(ALERT_RULE_NAMES) == set(RULE_IDS), (
        set(ALERT_RULE_NAMES) ^ set(RULE_IDS))
    signals = {signal_states.EVENT_TREND, signal_states.EVENT_CROSS,
               signal_states.EVENT_MOMENTUM}
    assert set(EVENT_RULE_NAMES) <= signals | _recorded_rule_literals()


def test_an_unknown_id_is_never_shown_as_itself() -> None:
    from portfolio_dash.shared.alert_rule_names import rule_name

    assert rule_name("bogus_rule") == _UNNAMED
    assert rule_name(None) == _UNNAMED
    assert rule_name("") == _UNNAMED
