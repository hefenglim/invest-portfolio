"""E2E (real browser) — DEF-062: the wizard's 監聽規則 checkboxes are all named, and in the
same words as 系統設定 › 預警規則 and › 通知中心.

The verifier's path on the demo (1ee7771): 洞察管線 › ＋新增洞察任務 → 觸發選「預警觸發」→ 監聽規則
listed 15 checkboxes, 8 of them raw ids (``missing_price``, ``drawdown_from_peak``, ``vol_spike``
…), while 預警規則 named all 15. This drives that path click by click and reads the labels the
owner sees, then reads the same rules off the two settings pages.
"""

import re

import pytest
from playwright.sync_api import ConsoleMessage, Page

from portfolio_dash.strategy.rules_config import RULE_IDS

_IDENT = re.compile(r"[a-z]+_[a-z_]+")

_WIZARD_RULES = """
() => Array.from(document.querySelectorAll('.wz-body .pv-check')).map((lb) => {
  const cb = lb.querySelector('input[type=checkbox]');
  return [cb ? cb.value : '', (lb.innerText || '').trim()];
})
"""
_SETTINGS_RULES = """
() => Array.from(document.querySelectorAll('#alert-rules-wrap .ar-row')).map((row) => {
  const tg = row.querySelector('.toggle[data-rule]');
  const nm = row.querySelector('.ar-name');
  return [tg ? tg.dataset.rule : '', nm ? (nm.innerText || '').trim() : ''];
})
"""
_NOTIFY_RULES = """
() => Array.from(document.querySelectorAll('#nt-subs .nt-sub')).map((row) => {
  const cb = row.querySelector('input[data-rule]');
  const spans = row.querySelectorAll('span');
  return [cb ? cb.dataset.rule : '', (spans[spans.length - 1].innerText || '').trim()];
})
"""


@pytest.mark.e2e
def test_the_wizard_names_every_rule_like_the_settings_pages(
    live_server: str, fresh_page: Page
) -> None:
    page = fresh_page
    warnings: list[str] = []

    def _on_console(msg: ConsoleMessage) -> None:
        if "DEF-062" in msg.text:
            warnings.append(msg.text)

    page.on("console", _on_console)

    # 洞察管線 › ＋新增洞察任務 → 觸發選「預警觸發」
    page.goto(live_server + "/pipeline-hub.html", wait_until="networkidle")
    page.locator("#pp-add").click()
    page.locator(".wz-opt", has_text="預警觸發").click()
    page.wait_for_function(
        "(n) => document.querySelectorAll('.wz-body .pv-check input[type=checkbox]').length"
        " >= n", arg=len(RULE_IDS))
    wizard = dict(page.evaluate(_WIZARD_RULES))
    assert list(wizard) == list(RULE_IDS), f"the wizard lists {list(wizard)}"
    blank = [rid for rid, label in wizard.items() if not label]
    raw = {rid: label for rid, label in wizard.items() if _IDENT.search(label)}
    unnamed = [rid for rid, label in wizard.items() if label == "未命名規則"]
    assert not blank, f"監聽規則 checkboxes with no label: {blank}"
    assert not raw, f"監聽規則 checkboxes showing a rule id: {raw}"
    assert not unnamed, f"監聽規則 checkboxes with no name on the wire: {unnamed}"

    # 系統設定 › 預警規則 — the same rule, the same words
    page.goto(live_server + "/settings.html#alerts", wait_until="networkidle")
    page.wait_for_selector("#alert-rules-wrap .ar-row .ar-name", state="visible")
    settings = dict(page.evaluate(_SETTINGS_RULES))
    differ = {rid: (wizard[rid], settings.get(rid)) for rid in RULE_IDS
              if settings.get(rid) != wizard[rid]}
    assert not differ, f"wizard vs 預警規則 name the same rule differently: {differ}"

    # 系統設定 › 通知中心 — its subscription list names the same rules the same way
    page.goto(live_server + "/settings.html#notify", wait_until="networkidle")
    page.wait_for_selector("#nt-subs .nt-sub", state="attached")
    notify = dict(page.evaluate(_NOTIFY_RULES))
    differ = {rid: (wizard[rid], notify.get(rid)) for rid in RULE_IDS
              if notify.get(rid) != wizard[rid]}
    assert not differ, f"wizard vs 通知中心 name the same rule differently: {differ}"

    assert not warnings, f"a renderer met a rule without a name: {warnings}"
