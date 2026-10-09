"""Alert-rule display names — ONE table, read by every layer that names a rule (DEF-062).

Before this module the zh name of an alert rule was written down three times, and the three
copies disagreed:

* ``web/pipeline-wizard.js`` ``RULE_LABELS`` — 7 of the 15 rules; ``ruleLabel(id)`` fell back
  to the id, so the 新增洞察任務 wizard's 「預警觸發」 step showed ``missing_price``,
  ``vol_spike``, ``target_cross`` … as eight of its fifteen checkboxes;
* ``web/settings-alerts.js`` ``META`` — all 15, the wording the owner reads on 系統設定 ›
  預警規則;
* ``ops/notify.py`` ``RULE_CATALOG`` — all 15 (+ signals / digests), for the push text.

They differed in wording as well as in coverage (``stale_price`` 「價格過期/缺價」 vs
「價格過期」, ``exdiv_upcoming`` 「即將除息」 vs 「即將除息提醒」). The settings page's wording
won (owner ruling in the DEF-062 spec: it is what the owner sees today) and now lives HERE.

**Why ``shared/``.** The name is read by layers that may not import each other: the api
wire (``GET /api/alert-rules`` carries ``name`` on every rule), ``ops/notify`` (push text —
``ops`` imports only ``shared``), ``llm_insight/gating`` (the R7 gate message — ``llm_insight``
may not import ``ops``), ``scheduler/jobs`` (the ``alert_scan`` run detail) and the digest.
``shared/`` is the one layer all of them may import, and it already carries the other
vocabularies (``cash_kinds``, ``corporate_actions``). Injecting the table from the
composition root was rejected for the reason D39 gives: a missed registration degrades to a
silently wrong label, here the very raw id this module exists to remove.

**The parent set is the registry, not this file.** ``strategy.rules_config.RULE_IDS`` owns
which alert rules exist; ``strategy.signal_states`` ``EVENT_*`` and the spec-04c
``calibration_regression`` event are the other ids that reach ``alert_events``.
``tests/contract/test_def062_alert_rule_names.py`` fails when a registered id has no name
here, or when a name here has no registered id.

**No renderer falls back to the id.** :func:`rule_name` answers :data:`UNNAMED_RULE` and logs
a warning for an id it does not know — a label that says "this has no name" is honest; a
raw ``snake_case`` identifier on the owner's screen or phone is the defect.
"""

import logging

logger = logging.getLogger(__name__)

#: What every renderer shows for a rule id with no name — never the id itself.
UNNAMED_RULE = "未命名規則"

#: The spec-03 alert rules, keyed by ``strategy.rules_config.RULE_IDS`` (same order). The
#: wording is the settings › 預警規則 page's as of 1ee7771.
ALERT_RULE_NAMES: dict[str, str] = {
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
    # ⚠ 「組合整體回撤」 must never collapse into 「回撤」: 高點回撤 above is PER-SYMBOL against
    # each name's own 52-week high, this one is the whole book (AI-D2 two-definitions trap).
    "portfolio_drawdown": "組合整體回撤",
    "currency_weight": "幣別集中度",
    "target_cross": "目標價穿越",
}

#: Ids recorded in ``alert_events`` that are NOT editable alert rules: the technical-signal
#: transitions (``strategy.signal_states`` ``EVENT_*``) and the spec-04c calibration event
#: (``api.insight_service._check_regression``). They reach the push, the digest, the run
#: detail and a card's 觸發 chip exactly like a rule does, so they need a name exactly like one.
EVENT_RULE_NAMES: dict[str, str] = {
    "signal_trend": "趨勢反轉",
    "signal_cross": "均線交叉",
    "signal_momentum": "動能轉向",
    "calibration_regression": "AI 成績轉差",
}


def rule_name(rule_id: str | None) -> str:
    """The zh display name of *rule_id* (an alert rule or an ``alert_events`` event id).

    An unknown or missing id answers :data:`UNNAMED_RULE` and logs a warning — never the id.
    """
    if rule_id:
        name = ALERT_RULE_NAMES.get(rule_id) or EVENT_RULE_NAMES.get(rule_id)
        if name:
            return name
    logger.warning("alert rule id %r has no display name (DEF-062)", rule_id)
    return UNNAMED_RULE
