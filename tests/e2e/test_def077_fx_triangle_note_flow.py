"""E2E — DEF-077 (owner ruling ⑥ A, 2026-09-26): the dashboard's 資料新鮮度 panel reads the FX
triangle's three states the way the server judged them — in words AND in colour.

Real server, real browser, the golden ledger (which reads USD/TWD, USD/MYR and MYR/TWD):

* **日期不同, consistent on the common date** — USD/TWD moved to 2026-06-10 while the other
  two legs are still on 2026-06-09, where 4.4 × 7 = 30.8 closes exactly. The note says
  「日期不同」, names each leg's date and the common date, reads 「（一致）」 and is NOT amber.
  Before DEF-077 this compared 31.5 with 30.8 across the two days and printed an amber
  「超過 0.05%」 with no data error anywhere (verifier J-01).
* **無法比較** — the legs share no date in the lookback: the note says so, neutrally.
* **超過 0.05%** — the golden ledger itself: all three legs on 2026-06-09, USD/TWD 33 against
  4.4 × 7 = 30.8. The guard fired, and only this state is the amber box.

R7 (verifier, 2026-09-26): R6 read ``n.style.color`` — the INLINE colour only — and asserted
it was empty. ``.fresh-note`` is amber by DEFAULT (styles.css), so the neutral states were
amber on screen while that assertion held. Every colour here is ``getComputedStyle`` — what
the reader sees — compared with the page's own ``--amber`` / ``--amber-soft`` resolved in the
same page, so a theme change moves both sides together.

The same scan found two more surfaces whose colour contradicted their state; both are pinned
here the same way:

* the 「推導」 tag on a derived cross rate used the 過期 badge's amber (owner 2026-09-26:
  provenance, not a warning → neutral);
* the AI 與額度 status chip set amber INLINE on 額度偏低 and cleared it only on 啟用中, so after
  a save re-rendered it as 已關閉 or 額度歸零 it kept the amber.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.pricing.results import FxRow
from portfolio_dash.pricing.store import upsert_fx
from portfolio_dash.shared.enums import Currency
from tests.conftest import GOLDEN_NOW, _seed_golden
from tests.e2e.conftest import FlowServerFactory

# The page's own warning tokens, resolved in the page (theme-proof). A probe element takes
# the var() through the same cascade the notes do.
_AMBER_JS = """() => {
  const p = document.createElement('div');
  p.style.color = 'var(--amber)';
  p.style.backgroundColor = 'var(--amber-soft)';
  document.body.appendChild(p);
  const cs = getComputedStyle(p);
  const out = [cs.color, cs.backgroundColor];
  p.remove();
  return out;
}"""
_STYLE_JS = "(n) => { const cs = getComputedStyle(n); return [cs.color, cs.backgroundColor]; }"


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed_usd_twd_a_day_ahead(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    upsert_fx(conn, [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=Decimal("30.8"), source="test"),
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 10),
              rate=Decimal("31.5"), source="test"),
        # MYR/TWD is the derived pair in production (pricing/cross.py) — label it so here,
        # so the 推導 tag renders next to the note it belongs with.
        FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=Decimal("7"), source="derived:USD"),
    ], fetched_at=GOLDEN_NOW)
    conn.commit()


def _seed_no_common_date(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    conn.execute("DELETE FROM fx_rates WHERE base='MYR' AND quote='TWD'")
    upsert_fx(conn, [FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=date(2026, 4, 1),
                           rate=Decimal("7"), source="test")], fetched_at=GOLDEN_NOW)
    conn.commit()


def _open_freshness(page: Page, base: str) -> None:
    page.goto(base + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")
    page.locator("#freshness > summary").click()


def _triangle_note(page: Page, base: str) -> tuple[str, list[str], list[str]]:
    _open_freshness(page, base)
    note = page.locator("#fresh-notes .fresh-note", has_text="匯率三角一致性")
    expect(note).to_have_count(1)
    expect(note).to_be_visible()
    shown: list[str] = note.evaluate(_STYLE_JS)
    amber: list[str] = page.evaluate(_AMBER_JS)
    return note.inner_text(), shown, amber


@pytest.mark.e2e
@pytest.mark.parametrize(("seed", "expected", "absent", "is_amber"), [
    (_seed_usd_twd_a_day_ahead,
     ("日期不同", "USD/TWD 2026-06-10", "USD/MYR 2026-06-09", "以共同日期 2026-06-09 比較",
      "（一致）"),
     ("超過 0.05%", "無法比較"), False),
    (_seed_no_common_date,
     ("無法比較", "沒有共同日期", "日期不同", "MYR/TWD 2026-04-01"),
     ("超過 0.05%", "（一致）"), False),
    (_seed_golden,
     ("超過 0.05%", "推得 30.800000", "直接報價 33.000000"),
     ("（一致）", "無法比較", "日期不同"), True),
], ids=["dates-differ-consistent", "no-common-date", "disagrees-on-common-date"])
def test_the_triangle_note_reads_the_servers_verdict(
    flow_server: FlowServerFactory, fresh_page: Page,
    seed: Callable[[sqlite3.Connection], None], expected: tuple[str, ...],
    absent: tuple[str, ...], is_amber: bool,
) -> None:
    base = flow_server(seed)
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    text, (color, background), (amber, amber_soft) = _triangle_note(page, base)
    for needle in expected:
        assert needle in text, f"missing 「{needle}」 in: {text}"
    for needle in absent:
        assert needle not in text, f"unexpected 「{needle}」 in: {text}"
    if is_amber:
        assert (color, background) == (amber, amber_soft), (
            f"the guard fired — this note must be the amber box, got color={color} "
            f"background={background}: {text}")
    else:
        assert color != amber, f"only a real disagreement is amber; text is {color}: {text}"
        assert background != amber_soft, (
            f"only a real disagreement sits on the amber box; background is {background}: {text}")
    assert not page_errors, page_errors


@pytest.mark.e2e
def test_the_derived_tag_is_provenance_not_a_warning(
    flow_server: FlowServerFactory, fresh_page: Page,
) -> None:
    """Owner 2026-09-26: 「推導」 says where a rate came from. It used the 過期 badge's amber, one
    column away from the real 過期 badges, so a derived rate as fresh as its legs read as stale."""
    base = flow_server(_seed_usd_twd_a_day_ahead)
    page = fresh_page
    _open_freshness(page, base)
    tag = page.locator("#fresh-fx .badge", has_text="推導")
    expect(tag).to_have_count(1)
    expect(tag).to_be_visible()
    color, background = tag.evaluate(_STYLE_JS)
    amber, amber_soft = page.evaluate(_AMBER_JS)
    assert color != amber and background != amber_soft, (
        f"「推導」 is painted as a warning: color={color} background={background}")


def _llm_config_router(state: dict[str, Any]) -> Callable[[Route], None]:
    def handle(route: Route) -> None:
        if route.request.method == "GET":
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(state["payload"]))
        else:
            route.continue_()
    return handle


@pytest.mark.e2e
@pytest.mark.parametrize(("change", "label", "colour_var"), [
    ("roles-cleared", "AI：已關閉", "--text-3"),
    ("quota-spent", "AI：額度歸零", "--up"),
], ids=["low-then-off", "low-then-zero"])
def test_the_ai_status_chip_does_not_keep_the_low_quota_amber(
    flow_server: FlowServerFactory, fresh_page: Page, change: str, label: str, colour_var: str,
) -> None:
    """The chip re-renders after every save (``boot()``). 額度偏低 used to set amber INLINE and
    only 啟用中 cleared it: 偏低 → 已關閉 stayed amber, 偏低 → 歸零 painted red over with amber.
    The GET is served from a copy of the real payload so the quota can be put anywhere; the
    save that triggers the reload is the real one."""
    base = flow_server(_seed_golden)
    page = fresh_page
    page.set_viewport_size({"width": 1280, "height": 900})
    real: dict[str, Any] = page.request.get(f"{base}/api/llm/config").json()
    low = copy.deepcopy(real)
    low["roles"] = {k: "demo" for k in real["roles"]}
    low["quota"]["remaining_usd"] = "0.50"
    low["quota"]["alert_threshold_usd"] = "1.00"
    after = copy.deepcopy(low)
    if change == "roles-cleared":
        after["roles"] = {k: None for k in real["roles"]}
    else:
        after["quota"]["remaining_usd"] = "0.00"
    state: dict[str, Any] = {"payload": low}
    page.route(lambda url: urlparse(url).path == "/api/llm/config",
               _llm_config_router(state))
    page.goto(f"{base}/settings.html#llm", wait_until="networkidle")
    chip = page.locator("#ai-status")
    expect(chip).to_contain_text("AI：額度偏低")
    amber, amber_soft = page.evaluate(_AMBER_JS)
    assert chip.evaluate(_STYLE_JS) == [amber, amber_soft], "偏低 itself is the amber chip"

    state["payload"] = after
    threshold = page.locator("#quota-threshold")
    with page.expect_response(
        lambda r: r.request.method == "GET" and "/api/llm/config" in r.url
    ):
        threshold.fill("2")
        threshold.dispatch_event("change")
    expect(chip).to_contain_text(label)
    color, background = chip.evaluate(_STYLE_JS)
    expected: str = page.evaluate(
        "(v) => { const p = document.createElement('span'); p.style.color = 'var(' + v + ')';"
        " document.body.appendChild(p); const c = getComputedStyle(p).color; p.remove();"
        " return c; }", colour_var)
    assert color != amber and background != amber_soft, (
        f"{label} kept 偏低's amber: color={color} background={background}")
    assert color == expected, f"{label} should read var({colour_var}) = {expected}, got {color}"
