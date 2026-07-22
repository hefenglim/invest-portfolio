"""Playwright UI driver: drives the REAL browser forms (trades.html, cash.html) and
reads back rendered DOM numbers for cross-checking. Used for the core happy paths.
"""

from __future__ import annotations

import re
from decimal import Decimal

from common import dec
from playwright.sync_api import sync_playwright


def _num(s: str) -> Decimal | None:
    """Strip thousands separators / currency glyphs from rendered text -> Decimal."""
    if s is None:
        return None
    m = re.findall(r"-?\d[\d,]*\.?\d*", s.replace("−", "-"))
    if not m:
        return None
    return Decimal(m[0].replace(",", ""))


class UiDriver:
    def __init__(self, base_url: str, headless: bool = True) -> None:
        self.base = base_url.rstrip("/")
        self.headless = headless

    def start(self) -> None:
        self._pw = sync_playwright().start()
        # ignore_https_errors for the demo TLS (Tailscale cert); harmless on localhost
        self.browser = self._pw.chromium.launch(headless=self.headless)
        self.ctx = self.browser.new_context(ignore_https_errors=True)
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(20000)

    def stop(self) -> None:
        try:
            self.page.close()
            self.ctx.close()
            self.browser.close()
        finally:
            self._pw.stop()

    # -------- write forms --------
    def manual_trade(self, account_id, symbol, side, d, shares, price):
        p = self.page
        p.goto(self.base + "/trades.html", wait_until="load")
        p.wait_for_function(
            "() => document.querySelector('#m-account') && "
            "document.querySelector('#m-account').options.length > 0")
        p.select_option("#m-account", account_id)
        p.fill("#m-symbol", symbol)
        p.click("#m-side-buy" if side == "buy" else "#m-side-sell")
        p.fill("#m-date", d)
        p.fill("#m-shares", str(shares))
        p.fill("#m-price", str(price))
        # wait for the server preview to enable the confirm button (no hard issues)
        p.wait_for_function(
            "() => { const b=document.querySelector('#m-confirm'); return b && !b.disabled; }")
        p.click("#m-confirm")

    def cash_move(self, account_id, kind, ccy, d, amount):
        p = self.page
        # FU-D25 split cash.html into tabs; #flows activates 出金入金 where the form lives.
        p.goto(self.base + "/cash.html#flows", wait_until="load")
        p.wait_for_function(
            "() => document.querySelector('#cm-account') && "
            "document.querySelector('#cm-account').options.length > 0")
        p.select_option("#cm-account", account_id)
        p.fill("#cm-date", d)
        p.click("#cm-kind-in" if kind == "deposit" else "#cm-kind-out")
        p.select_option("#cm-ccy", ccy)
        p.fill("#cm-amount", str(amount))
        p.click("#cm-confirm")

    def fx(self, account_id, d, from_ccy, from_amt, to_ccy, to_amt):
        p = self.page
        # FU-D25: the FX form lives under the 換匯中心 tab (#fx).
        p.goto(self.base + "/cash.html#fx", wait_until="load")
        p.wait_for_function(
            "() => document.querySelector('#cfx-account') && "
            "document.querySelector('#cfx-account').options.length > 0")
        p.select_option("#cfx-account", account_id)
        # R6-D: a single-currency account (e.g. tw_broker) disables the ccy/amount
        # controls -- callers MUST pass a two-currency account (schwab / moomoo_my).
        # Wait for the controls to actually be enabled before touching them: the
        # account switch's re-sync (clear amounts, dedupe ccy options, single-ccy
        # gate) now runs on every #cfx-account change, and the default first option in
        # the dropdown (tw_broker) is single-currency, so this also fails fast with a
        # clear message if a caller ever passes a single-currency account by mistake,
        # instead of a confusing Playwright action-timeout inside select_option below.
        p.wait_for_function("() => !document.querySelector('#cfx-from-ccy').disabled")
        p.fill("#cfx-date", d)
        p.select_option("#cfx-from-ccy", from_ccy)
        p.fill("#cfx-from-amt", str(from_amt))
        p.select_option("#cfx-to-ccy", to_ccy)
        p.fill("#cfx-to-amt", str(to_amt))
        p.click("#cfx-confirm")

    def manual_dividend(self, account_id, model, symbol, d, gross, reinvest_price=None,
                        net=None):
        """Drive the 股利 tab form (account-model-dependent sub-form)."""
        p = self.page
        p.goto(self.base + "/trades.html", wait_until="load")
        p.wait_for_function(
            "() => document.querySelector('#d-account') && "
            "document.querySelector('#d-account').options.length > 0")
        p.click("#tab-div")
        p.select_option("#d-account", account_id)
        p.fill("#d-symbol", symbol)
        p.fill("#d-date", d)
        if model == "tw":
            p.wait_for_selector("#d-tw-gross", state="visible")
            p.fill("#d-tw-gross", str(gross))
            if net is not None:
                p.fill("#d-tw-net", str(net))
        elif model == "drip":
            p.wait_for_selector("#d-drip-gross", state="visible")
            p.fill("#d-drip-gross", str(gross))
            if reinvest_price is not None:
                p.fill("#d-drip-price", str(reinvest_price))
        else:  # net (MY)
            p.wait_for_selector("#d-net-amt", state="visible")
            p.fill("#d-net-amt", str(gross))
        p.click("#d-confirm")

    INBOX_PATH = "/dividend-inbox.html"

    def inbox_refresh(self):
        """Navigate to the dividend-inbox page and click 重新偵測 (real provider scan)."""
        p = self.page
        p.goto(self.base + self.INBOX_PATH, wait_until="load")
        p.wait_for_selector("#inbox-section")
        btn = p.get_by_role("button", name="重新偵測")
        btn.click()
        # wait for busy state to clear (button text returns) or timeout
        try:
            p.wait_for_function(
                "() => { const b=[...document.querySelectorAll('button')]"
                ".find(x=>x.textContent.includes('重新偵測')); return b && !b.disabled; }",
                timeout=60000)
        except Exception:
            pass

    def inbox_confirm(self, max_confirm=3):
        """Click up to max_confirm 確認入帳 buttons. Groups are collapsed <details> when
        there are >3 symbols, so force them open and re-query after each re-render."""
        p = self.page
        confirmed = 0
        for _ in range(max_confirm):
            p.eval_on_selector_all("#inbox-list details", "els=>els.forEach(e=>e.open=true)")
            target = None
            for b in p.query_selector_all("#inbox-list button"):
                try:
                    if (b.inner_text().strip() == "確認入帳" and b.is_enabled()
                            and b.is_visible()):
                        target = b
                        break
                except Exception:
                    continue
            if target is None:
                break
            try:
                target.click()
            except Exception:
                break
            p.wait_for_timeout(2500)  # confirm POST + boot() re-render settle
            confirmed += 1
        return confirmed

    def page_errors_on(self, path, settle_ms=2500):
        """Navigate to path and return uncaught JS page errors observed during load."""
        errs: list[str] = []
        handler = lambda e: errs.append(str(e))  # noqa: E731
        self.page.on("pageerror", handler)
        try:
            self.page.goto(self.base + path, wait_until="load")
            self.page.wait_for_timeout(settle_ms)
        finally:
            self.page.remove_listener("pageerror", handler)
        return errs

    def inbox_pending_count(self):
        el = self.page.query_selector("#inbox-count")
        try:
            return int((el.inner_text() or "0").strip())
        except Exception:
            return 0


def dom_readback(ui: UiDriver, ev, api):
    """Read browser-rendered numbers and compare to the API-computed values.

    Covers: dashboard KPI band, 3 holdings rows, cash balance cards, one realized row.
    Rendered cells carry thousands separators / display quantization, so the check is
    'rendered value == API value quantized to the currency minor unit' (display parity),
    NOT full-precision equality (which only the CSV/JSON channels guarantee).
    """
    p = ui.page
    phase = "phase1:dom"

    # ---- dashboard ----
    dash = api.get("/api/dashboard").json()
    p.goto(ui.base + "/index.html", wait_until="load")
    p.wait_for_function("() => document.querySelectorAll('#holdings-body tr').length > 0")

    # KPI band: match the total_market_value figure somewhere in the band text
    band = p.inner_text("#kpi-band")
    tmv = dash["kpis"].get("total_market_value")
    if tmv is not None:
        want = _q(dec(tmv), "TWD")
        present = _contains_number(band, want)
        ev.check("dom.kpi.total_market_value_present", "index #kpi-band", True, present, phase)

    # 3 holdings rows: shares should render for each
    rows = p.query_selector_all("#holdings-body tr")
    app_hold = {(h["account_id"], h["symbol"]): h for h in dash["holdings"]}
    checked = 0
    for tr in rows:
        txt = tr.inner_text()
        # find a holding whose symbol appears in the row and whose shares match
        for (aid, sym), h in app_hold.items():
            if sym and (sym in txt):
                shares_disp = _num_after_symbol(txt, sym)
                # the row shows many numbers; assert the shares value appears in the row
                want_sh = dec(h["shares"])
                if _contains_number(txt, want_sh) or shares_disp == want_sh:
                    ev.check("dom.holding.shares_present", f"{aid}/{sym}", True, True, phase)
                    checked += 1
                break
        if checked >= 3:
            break
    ev.check("dom.holdings.rows_checked", "index #holdings-body", True, checked >= 3, phase)

    # one realized row present
    rrows = p.query_selector_all("#realized-body tr")
    if dash["realized"]["rows"]:
        r0 = dash["realized"]["rows"][0]
        found = False
        for tr in rrows:
            t = tr.inner_text()
            if r0["symbol"] in t:
                found = True
                break
        ev.check("dom.realized.row_present", f"{r0['symbol']}", True, found, phase)

    # ---- cash cards ----
    cash = api.get("/api/cash", limit=500).json()
    p.goto(ui.base + "/cash.html", wait_until="load")
    p.wait_for_function("() => document.querySelectorAll('#cash-cards .cash-card').length > 0")
    cards_text = p.inner_text("#cash-cards")
    checked_c = 0
    for b in cash["balances"]:
        want = _q(dec(b["amount"]), b["ccy"])
        if _contains_number(cards_text, want):
            checked_c += 1
    ev.check("dom.cash.cards_match", "cash #cash-cards",
             True, checked_c >= min(3, len(cash["balances"])), phase)


def _q(value: Decimal, ccy: str) -> Decimal:
    from decimal import ROUND_HALF_UP
    places = {"TWD": 0, "USD": 2, "MYR": 2}.get(ccy, 2)
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _contains_number(text: str, value: Decimal) -> bool:
    """True if the display of `value` (with thousands separators) appears in text."""
    av = abs(value)
    forms = set()
    for dp in (0, 2):
        q = av.quantize(Decimal(1).scaleb(-dp)) if dp else av.quantize(Decimal(1))
        s = f"{q:,.{dp}f}"
        forms.add(s)
        forms.add(s.replace(",", ""))
    norm = text.replace("−", "-").replace(",", "")
    for f in forms:
        if f.replace(",", "") in norm:
            return True
    return False


def _num_after_symbol(txt: str, sym: str) -> Decimal | None:
    idx = txt.find(sym)
    if idx < 0:
        return None
    return _num(txt[idx + len(sym):])
