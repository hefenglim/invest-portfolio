"""E2E DEF-030 / DEF-037: the browser shows what the server now says.

DEF-030 — 排程中心: an ``insight:<id>`` row is named by its TASK (the server's ``label``) in
the job table AND in the run history, and a task paused in the 洞察管線 shows its schedule
switch OFF and locked (``effective_enabled`` / ``paused_reason``), with the way back.
Measured before: the row read 「insight:10」 and the switch read 「啟用」 for a paused task.

DEF-037 — AI 洞察: an alert card carries 「由預警「<規則>」觸發」, linking to the 預警規則
settings, with the alert's own title/detail in its tooltip; a card without a trigger shows
none. Measured before: nothing on a card said which alert produced it.

Both pages run their REAL scripts; only the JSON is stubbed (``page.route``), and every
route is removed in ``finally`` — the browser page is session-scoped.
"""

import json

from playwright.sync_api import Page, Route

_JOBS = {
    "jobs": [
        {"id": "quotes_tw", "desc": "TW quotes + FX (post-close)", "kind": "system",
         "label": None, "cron": "0 14 * * mon-fri", "tz": "Asia/Taipei", "enabled": True,
         "effective_enabled": True, "paused_reason": None, "last": None, "next": None},
        {"id": "insight:10", "desc": "insight:10", "kind": "insight",
         "label": "AI 洞察任務「每日持倉週報」", "cron": "0 8 * * *", "tz": "Asia/Taipei",
         "enabled": True, "effective_enabled": True, "paused_reason": None,
         "last": None, "next": None},
        {"id": "insight:7", "desc": "insight:7", "kind": "insight",
         "label": "AI 洞察任務「暫停中的任務」", "cron": "0 9 * * *", "tz": "Asia/Taipei",
         "enabled": True, "effective_enabled": False,
         "paused_reason": "任務已在 AI 洞察管線暫停，排程觸發時不會執行；請至洞察管線啟用",
         "last": None, "next": None},
    ],
    "scheduler": {"running": False, "reason": "PD_DISABLE_SCHEDULER=1"},
}
_RUNS = {
    "rows": [{"id": 153, "job_id": "insight:10", "label": "AI 洞察任務「每日持倉週報」",
              "started_at": "2026-09-23T09:00:00+08:00",
              "finished_at": "2026-09-23T09:00:05+08:00", "status": "ok", "detail": "",
              "duration_s": 5.0, "cost_usd": None}],
    "total_count": 1,
}


def _json(body: object):  # type: ignore[no-untyped-def]
    def handler(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    return handler


def test_scheduler_center_names_insight_rows_and_locks_a_paused_task(
    live_server: str, browser_page: Page
) -> None:
    page = browser_page
    page.route("**/api/scheduler/jobs", _json(_JOBS))
    page.route("**/api/scheduler/runs*", _json(_RUNS))
    try:
        page.goto(live_server + "/settings.html#scheduler", wait_until="load")
        page.wait_for_selector("#jobs-body tr", state="attached")
        page.wait_for_selector("#hist-body tr", state="attached")
        got = page.evaluate("""() => {
            const rows = [...document.querySelectorAll('#jobs-body tr')];
            const byId = {};
            rows.forEach((tr) => {
              const id = tr.querySelector('.cron-code').textContent;
              const tog = tr.querySelector('button.toggle');
              const note = tr.querySelector('.paused-note');
              byId[id] = {
                name: tr.querySelector('td').firstChild.textContent,
                on: tog.classList.contains('on'), disabled: tog.disabled, title: tog.title,
                note: note ? note.textContent : null,
                link: note && note.querySelector('a')
                  ? note.querySelector('a').getAttribute('href') : null,
              };
            });
            const hist = document.querySelector('#hist-body tr td:nth-child(2) div').textContent;
            return { byId, hist };
        }""")
    finally:
        page.unroute("**/api/scheduler/jobs")
        page.unroute("**/api/scheduler/runs*")
    live = got["byId"]["insight:10"]
    assert live["name"] == "AI 洞察任務「每日持倉週報」", got
    assert live["on"] is True and live["disabled"] is False and live["note"] is None
    paused = got["byId"]["insight:7"]
    assert paused["name"] == "AI 洞察任務「暫停中的任務」", got
    assert paused["on"] is False and paused["disabled"] is True, got
    assert "暫停" in paused["title"] and "暫停" in (paused["note"] or ""), got
    assert paused["link"] == "pipeline-hub.html", got
    assert got["byId"]["quotes_tw"]["name"] == "台股報價＋匯率（收盤後）"   # JOB_ZH unchanged
    assert got["hist"] == "AI 洞察任務「每日持倉週報」", got


def _card(title: str, trigger: object) -> dict[str, object]:
    return {"id": 1 if trigger else 2, "insight_type_id": 3, "symbol": "2884",
            "is_shadow": False, "calibration_version": None, "title": title,
            "summary": "s", "body_md": "b", "tags": [], "confidence": None,
            "prediction": None, "unreadable": False,
            "figure_flags": {"unverified_figures": [], "unknown_symbols": [],
                             "snapshot": "ok"},
            "horizon_days": 3, "due_at": None, "model": "m", "cost_usd": "0",
            "tokens_in": 0, "tokens_out": 0, "created_at": "2026-09-23T15:00:00+08:00",
            "trigger": trigger}


_TRIGGER = {"source": "alert", "rule": "target_cross", "rule_label": "目標價穿越",
            "alert_id": 42, "fired_at": "2026-09-23T15:00:00+08:00", "scope": "symbol",
            "subject": "2884", "title": "2884 跌破目標價", "detail": "現價 48.5 ≤ 目標下限 50"}


def _insights(route: Route) -> None:
    url = route.request.url
    if "group=symbol" in url:
        body: object = {"groups": [{"symbol": "2884", "total": 2, "cards": [
            _card("2884 提點", _TRIGGER), _card("2884 舊卡", None)]}],
            "total_count": 1, "limit": 25, "offset": 0, "history_limit": 5}
    else:
        body = {"rows": [], "total_count": 0, "limit": 25, "offset": 0}
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def test_an_alert_card_says_which_alert_produced_it(
    live_server: str, browser_page: Page
) -> None:
    page = browser_page
    page.route("**/api/insights*", _insights)
    try:
        page.goto(live_server + "/insights.html", wait_until="load")
        page.wait_for_selector("#ins-health-grid .hc-card", state="attached")
        got = page.evaluate("""() => {
            const cards = [...document.querySelectorAll('#ins-health-grid .hc-card')];
            return cards.map((c) => {
              const t = c.querySelector('.insight-trigger');
              return { title: c.querySelector('.hc-conclusion').textContent,
                       text: t ? t.textContent : null,
                       href: t ? t.getAttribute('href') : null,
                       tip: t ? t.title : null };
            });
        }""")
    finally:
        page.unroute("**/api/insights*")
    by_title = {c["title"]: c for c in got}
    fired = by_title["2884 提點"]
    assert fired["text"] == "由預警「目標價穿越」觸發", got
    assert fired["href"] == "settings.html#alerts", got
    assert "2884 跌破目標價" in fired["tip"] and "現價 48.5 ≤ 目標下限 50" in fired["tip"], got
    assert by_title["2884 舊卡"]["text"] is None, got   # a legacy card claims nothing
