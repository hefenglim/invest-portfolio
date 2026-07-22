"""E2E (Playwright, real server + real frontend): the merged ``moomoo_my`` account END-TO-END.

Batch B (Moomoo merge, 2026-07-21) collapses ``moomoo_my_us`` + ``moomoo_my_my`` into ONE
dual-market account ``moomoo_my`` (settlement USD / funding MYR; markets US+MY; per-market
fee + dividend bindings — US→moomoo_us/DRIP, MY→moomoo_my/cash). These browser flows drive
the REAL stack (uvicorn subprocess + on-disk SQLite + served ``web/`` + live compute) to prove
the merged account behaves correctly per market, with ZERO console / page errors unless a 4xx
is intrinsic to the flow (then only the documented-benign network line is filtered).

Coverage (Wave 6, T13):
  1. Dual-market trading + cash split — a US buy drains the USD pool (MYR untouched); an MY
     buy drains the MYR pool (USD untouched); the preview fee ROUTES per market (exact
     Decimal, cross-checked against the market-appropriate rule set).
  2. Dividend split (F01) — one US-symbol dividend on moomoo_my commits a ``DRIP`` row (30%
     withholding); one MY-symbol dividend commits a ``NET`` row (net cash). Ledger readback.
  3. Preview-ccy pins (F06) — an MY draft shows MYR money + 3-dp price; a US draft shows USD.
  4. Migration-produced DB — a LEGACY-shaped DB passed through ``migrate_moomoo_accounts`` in
     the seed hook serves the cash trio (MYR deposit + USD deposit + MYR→USD conversion)
     identically to a natively-seeded DB.
  5. Legacy-CSV alias (browser) — a CSV carrying ``moomoo_my_us`` previews the 已合併 alias
     notice and commits onto ``moomoo_my``.
  6. FX form boot-enabled + single-ccy gate retained — the default-first ``moomoo_my`` boots
     the FX form ENABLED (dual-ccy); tw_broker (TWD-only) trips the single-ccy disable gate,
     which a forced re-boot can never leak back to enabled.
  7. §8.3 both-order pins — (a) MYR→USD conversion first then an over-pool MY buy raises the
     ACK-able ``cash_overdraft`` soft gate (commit only after ack); (b) MY buy first then an
     over-pool MYR→USD conversion is a HARD block (inline error + confirm disabled, NO ack).
"""

import json
import urllib.request
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from playwright.sync_api import Page, expect
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set, seed_accounts
from portfolio_dash.data_ingestion.fees import compute_fees
from portfolio_dash.data_ingestion.moomoo_merge import migrate_moomoo_accounts
from portfolio_dash.data_ingestion.store import (
    insert_cash_movement,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import _seed_golden
from tests.e2e.conftest import FlowServerFactory


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets PER TEST (pytest-socket re-bans before every test); each flow
    spawns a fresh isolated uvicorn (free-port probe + readiness poll need loopback TCP)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


# --- shared helpers -----------------------------------------------------------------------

_BUY_DATE = "2026-06-15"  # past (no future-trade warning) + on/after the golden USD/MYR rate
#                            so the Moomoo-US MY stamp resolves (no stamp_fx_missing soft gate).


def _get_json(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + path, timeout=5) as r:  # noqa: S310 (loopback)
        data: dict[str, Any] = json.loads(r.read().decode("utf-8"))
        return data


def _cash_balance(base: str, account_id: str, ccy: str) -> Decimal | None:
    for b in _get_json(base, "/api/cash")["balances"]:
        if b["account_id"] == account_id and b["ccy"] == ccy:
            return Decimal(str(b["amount"]))
    return None


def _holding(base: str, symbol: str, account_id: str) -> dict[str, Any] | None:
    rows: list[dict[str, Any]] = _get_json(base, "/api/dashboard")["holdings"]
    for h in rows:
        if h["symbol"] == symbol and h["account_id"] == account_id:
            return h
    return None


def _dividend_rows(base: str, account_id: str, symbol: str) -> list[dict[str, Any]]:
    path = f"/api/ledgers/dividends?account_id={account_id}&symbol={symbol}"
    rows: list[dict[str, Any]] = _get_json(base, path)["rows"]
    return rows


def _sink(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(getattr(m, "text", ""))
            if getattr(m, "type", None) == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    return console_errors, page_errors


def _reg_my(conn: Any) -> None:
    """Register a MY-market MYR counter (1155 Maybank) for the merged account's MY leg."""
    upsert_instrument(conn, Instrument(symbol="1155", market=Market.MY, quote_ccy=Currency.MYR,
                                       sector="Banks", name="Maybank", board=".KL"))


def _open_manual(page: Page, base: str) -> None:
    """trades.html with the default 手動交易 pane mounted (accounts dropdown populated)."""
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#m-account option", state="attached")


def _manual_preview(
    page: Page, *, account: str, symbol: str, shares: str, price: str, side: str = "buy",
) -> dict[str, Any]:
    """Fill the manual draft and return the SERVER preview JSON.

    ``runManualPreview`` only POSTs once symbol + shares > 0 + price > 0 (earlier fills render
    local-issue states with NO request), so the single preview POST is the one the price fill
    triggers — captured deterministically here (spec-17 §17.7.4: expect-polling, no sleeps).
    """
    page.select_option("#m-account", account)
    if side == "sell":
        page.click("#m-side-sell")
    else:
        page.click("#m-side-buy")
    page.fill("#m-date", _BUY_DATE)
    page.fill("#m-symbol", symbol)
    page.fill("#m-shares", shares)
    with page.expect_response("**/api/input/manual/preview") as pv:
        page.fill("#m-price", price)
    assert pv.value.status == 200, f"manual preview status {pv.value.status}"
    body: dict[str, Any] = pv.value.json()
    # Sanity: this is the priced preview (all fields set), not a stale earlier request.
    assert Decimal(body["gross"]) == Decimal(shares) * Decimal(price), (
        f"captured a stale preview: gross={body['gross']!r}"
    )
    return body


def _commit_manual(page: Page) -> None:
    with page.expect_response("**/api/input/manual/commit") as cm:
        page.click("#m-confirm")
    assert cm.value.status == 201, f"manual commit status {cm.value.status}"
    page.wait_for_selector(".toast-ok")


def _open_div(page: Page, base: str) -> None:
    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#d-account option", state="attached")
    page.click("#tab-div")
    page.wait_for_selector("#d-symbol", state="visible")


def _commit_dividend(page: Page) -> None:
    with page.expect_response("**/api/import/commit") as cm:
        page.click("#d-confirm")
    assert cm.value.status == 200, f"dividend commit status {cm.value.status}"
    page.wait_for_selector(".toast-ok")


# =========================================================================================
# 1. Dual-market trading + per-market cash split + per-market fee routing
# =========================================================================================

def _seed_dual_market_cash(conn: Any) -> None:
    """Merged moomoo_my funded in BOTH pools (USD + MYR) + a registered MY counter.

    _seed_golden gives the merged account, AAPL (US), and the USD/MYR 4.4 rate the US-market
    MY-stamp resolves against; 1155 (MY) is the MY-market leg. Both pools start well-funded so
    neither buy trips the cash_overdraft soft gate."""
    _seed_golden(conn)
    _reg_my(conn)
    insert_cash_movement(conn, account_id="moomoo_my", move_date=date(2026, 1, 5),
                         kind="DEPOSIT", ccy=Currency.USD, amount=Decimal("100000"))
    insert_cash_movement(conn, account_id="moomoo_my", move_date=date(2026, 1, 5),
                         kind="DEPOSIT", ccy=Currency.MYR, amount=Decimal("100000"))
    conn.commit()


@pytest.mark.e2e
def test_dual_market_buy_splits_cash_and_routes_fees_per_market(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """A US buy hits only the USD pool; an MY buy hits only the MYR pool; each preview fee is
    the market-appropriate rule's exact figure (moomoo_us vs moomoo_my)."""
    base = flow_server(_seed_dual_market_cash)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    # independently-derived per-market fee expectations (the SAME pure engine the API resolves
    # per market — comparing the priced preview to each rule set PROVES which rule routed).
    exp_us_fee = compute_fees(
        get_fee_rule_set("moomoo_us"), Side.BUY, Decimal("10"), Decimal("100")).fee
    exp_my_fee = compute_fees(
        get_fee_rule_set("moomoo_my"), Side.BUY, Decimal("1000"), Decimal("9")).fee
    assert exp_us_fee != exp_my_fee, "the two market rule sets must yield different fees"

    usd0 = _cash_balance(base, "moomoo_my", "USD")
    myr0 = _cash_balance(base, "moomoo_my", "MYR")
    assert usd0 == Decimal("100000") and myr0 == Decimal("100000")

    _open_manual(page, base)

    # ---- US buy (AAPL) -> moomoo_us fee; USD pool falls, MYR untouched --------------------
    us = _manual_preview(page, account="moomoo_my", symbol="AAPL", shares="10", price="100")
    assert Decimal(us["fee"]) == exp_us_fee, f"US fee routed wrong: {us['fee']!r}"
    assert Decimal(us["fee"]) != exp_my_fee
    _commit_manual(page)

    usd1 = _cash_balance(base, "moomoo_my", "USD")
    myr1 = _cash_balance(base, "moomoo_my", "MYR")
    assert usd1 is not None and usd1 < usd0, "US buy must drain the USD pool"
    assert myr1 == myr0, "US buy must NOT touch the MYR pool"

    # ---- MY buy (1155) -> moomoo_my fee; MYR pool falls, USD untouched -------------------
    my = _manual_preview(page, account="moomoo_my", symbol="1155", shares="1000", price="9")
    assert Decimal(my["fee"]) == exp_my_fee, f"MY fee routed wrong: {my['fee']!r}"
    assert Decimal(my["fee"]) != exp_us_fee
    _commit_manual(page)

    usd2 = _cash_balance(base, "moomoo_my", "USD")
    myr2 = _cash_balance(base, "moomoo_my", "MYR")
    assert myr2 is not None and myr2 < myr1, "MY buy must drain the MYR pool"
    assert usd2 == usd1, "MY buy must NOT touch the USD pool"

    assert not console_errors and not page_errors, (
        f"dual-market cash split: console={console_errors!r} page={page_errors!r}"
    )


# =========================================================================================
# 2. Dividend split (F01): one US symbol -> DRIP, one MY symbol -> NET, on ONE account
# =========================================================================================

def _seed_div_split(conn: Any) -> None:
    """moomoo_my holding a US symbol (AAPL) and an MY symbol (1155), so each dividend model
    has a live target on the ONE merged account."""
    _seed_golden(conn)
    _reg_my(conn)
    insert_transaction(conn, account_id="moomoo_my", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_transaction(conn, account_id="moomoo_my", symbol="1155", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("9"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 11))
    conn.commit()


@pytest.mark.e2e
def test_dividend_split_us_drip_and_my_net_on_merged_account(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """On moomoo_my: a US-symbol dividend commits type=DRIP (30% withholding); an MY-symbol
    dividend commits type=NET (net cash). The dividend form follows the SYMBOL's market (F01)."""
    base = flow_server(_seed_div_split)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    _open_div(page, base)
    page.select_option("#d-account", "moomoo_my")

    # ---- US symbol AAPL -> the DRIP block (multi-market form follows the entered symbol) --
    page.fill("#d-symbol", "AAPL")
    page.wait_for_selector("#d-drip", state="visible")
    page.wait_for_selector("#d-net", state="hidden")
    page.fill("#d-date", "2026-07-10")
    page.fill("#d-drip-gross", "100")
    page.wait_for_function("() => document.querySelector('#d-drip-wh').value === '30.00'")
    page.wait_for_function("() => document.querySelector('#d-drip-net').value === '70.00'")
    page.fill("#d-drip-shares", "0.5")
    page.fill("#d-drip-price", "140")
    _commit_dividend(page)

    drip = [d for d in _dividend_rows(base, "moomoo_my", "AAPL") if d["type"] == "drip"]
    assert len(drip) == 1, f"expected exactly one DRIP row, got {drip!r}"
    assert Decimal(drip[0]["withhold"]) == Decimal("30")     # 100 × 30% US withholding
    assert Decimal(drip[0]["net"]) == Decimal("70")
    assert Decimal(drip[0]["reinvest_shares"]) == Decimal("0.5")

    # ---- MY symbol 1155 -> the NET block (same account, different market) ----------------
    page.fill("#d-symbol", "1155")
    page.wait_for_selector("#d-net", state="visible")
    page.wait_for_selector("#d-drip", state="hidden")
    page.fill("#d-net-amt", "200")
    _commit_dividend(page)

    net = [d for d in _dividend_rows(base, "moomoo_my", "1155") if d["type"] == "net"]
    assert len(net) == 1, f"expected exactly one NET row, got {net!r}"
    assert Decimal(net[0]["net"]) == Decimal("200")          # MY single-tier net cash received
    assert Decimal(net[0]["withhold"]) == Decimal("0")

    assert not console_errors and not page_errors, (
        f"dividend split F01: console={console_errors!r} page={page_errors!r}"
    )


# =========================================================================================
# 3. Preview-ccy pins (F06): MY draft -> MYR + 3-dp price; US draft -> USD
# =========================================================================================

def _seed_ccy_pins(conn: Any) -> None:
    """moomoo_my holding AAPL (US, avg 100) + 1155 (MY, avg 9) so the preview card's position
    what-if renders a KNOWN original-avg (100.00 for USD 2-dp, 9.000 for MYR 3-dp)."""
    _seed_golden(conn)
    _reg_my(conn)
    insert_transaction(conn, account_id="moomoo_my", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_transaction(conn, account_id="moomoo_my", symbol="1155", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("9"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 11))
    conn.commit()


@pytest.mark.e2e
def test_preview_ccy_and_precision_follow_the_symbol_market(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """The manual preview card's money label + price precision pin to the RESOLVED symbol's
    quote ccy: an MY draft on moomoo_my shows MYR + 3-dp prices, a US draft shows USD + 2-dp."""
    base = flow_server(_seed_ccy_pins)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    _open_manual(page, base)

    # ---- MY draft (1155) -> MYR-labelled money + 3-dp original-avg ("9.000") -------------
    _manual_preview(page, account="moomoo_my", symbol="1155", shares="1000", price="9")
    expect(page.locator("#m-pc-ccy")).to_have_text("MYR")
    orig_my = page.locator("#m-pc-rows .pc-row", has_text="原始均價")
    expect(orig_my).to_be_visible()
    assert "9.000" in (orig_my.inner_text()), (
        f"MY price must render 3-dp: {orig_my.inner_text()!r}"
    )
    assert "MYR" in page.inner_text("#m-pc-rows"), "money rows must carry the MYR label"

    # ---- US draft (AAPL) -> USD-labelled money + 2-dp original-avg ("100.00") ------------
    _manual_preview(page, account="moomoo_my", symbol="AAPL", shares="10", price="100")
    expect(page.locator("#m-pc-ccy")).to_have_text("USD")
    orig_us = page.locator("#m-pc-rows .pc-row", has_text="原始均價")
    expect(orig_us).to_be_visible()
    assert "100.00" in (orig_us.inner_text()), (
        f"US price must render 2-dp: {orig_us.inner_text()!r}"
    )
    assert "USD" in page.inner_text("#m-pc-rows"), "money rows must carry the USD label"

    assert not console_errors and not page_errors, (
        f"preview ccy pins F06: console={console_errors!r} page={page_errors!r}"
    )


# =========================================================================================
# 4. Migration-produced DB: a legacy-shaped DB, migrated in the seed hook, serves natively
# =========================================================================================

def _insert_legacy_account(
    conn: Any, account_id: str, name: str, settlement: str, funding: str,
    fee_rule_set: str, dividend_model: str,
) -> None:
    conn.execute(
        "INSERT INTO accounts (account_id, name, broker, settlement_ccy, funding_ccy, "
        "fee_rule_set, dividend_model) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (account_id, name, name, settlement, funding, fee_rule_set, dividend_model),
    )


def _seed_migrated(conn: Any) -> None:
    """Build a LEGACY-shaped DB (both pre-merge Moomoo accounts + a legacy MYR deposit) then run
    the REAL boot migration in the seed hook. Post-migration the app must serve moomoo_my with
    the relabelled legacy flow exactly as a natively-seeded merged account would."""
    seed_accounts(conn)  # current topology incl. the merged moomoo_my (+ its market bindings)
    _insert_legacy_account(conn, "moomoo_my_us", "Moomoo 美股", "USD", "MYR",
                           "moomoo_us", "drip_us")
    _insert_legacy_account(conn, "moomoo_my_my", "Moomoo 馬股", "MYR", "MYR",
                           "moomoo_my", "cash")
    # a legacy MYR deposit that must relabel onto moomoo_my (cash-pool continuity guard).
    insert_cash_movement(conn, account_id="moomoo_my_my", move_date=date(2026, 1, 2),
                         kind="DEPOSIT", ccy=Currency.MYR, amount=Decimal("30000"))
    conn.commit()
    performed = migrate_moomoo_accounts(conn)
    assert performed is True, "the seed hook must actually run the moomoo merge"
    conn.commit()


def _cash_deposit(page: Page, base: str, *, ccy: str, amount: str) -> None:
    page.goto(base + "/cash.html#flows", wait_until="load")
    page.wait_for_selector("#cm-account option", state="attached")
    page.select_option("#cm-account", "moomoo_my")
    page.click("#cm-kind-in")
    page.select_option("#cm-ccy", ccy)
    page.fill("#cm-amount", amount)
    with page.expect_response("**/api/cash/movements") as cm:
        page.click("#cm-confirm")
    assert cm.value.status == 201, f"deposit status {cm.value.status}"
    page.wait_for_selector(".toast-ok")


def _fx_convert(page: Page, base: str, *, from_amt: str, to_amt: str) -> None:
    page.goto(base + "/cash.html#fx", wait_until="load")
    page.wait_for_selector("#cfx-account option", state="attached")
    page.select_option("#cfx-account", "moomoo_my")
    page.wait_for_function("() => !document.querySelector('#cfx-from-ccy').disabled")
    page.fill("#cfx-from-amt", from_amt)
    page.fill("#cfx-to-amt", to_amt)
    page.wait_for_function("() => !document.querySelector('#cfx-confirm').disabled")
    with page.expect_response("**/api/cash/fx") as cm:
        page.click("#cfx-confirm")
    assert cm.value.status == 201, f"fx convert status {cm.value.status}"
    page.wait_for_selector(".toast-ok")


@pytest.mark.e2e
def test_migrated_db_serves_cash_trio_identically(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """A DB built legacy-shaped then passed through migrate_moomoo_accounts serves the cash
    trio (MYR deposit + USD deposit + MYR→USD conversion) on the merged moomoo_my account."""
    base = flow_server(_seed_migrated)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    # the relabelled legacy MYR deposit is on the merged account (migration ran in the seed).
    assert _cash_balance(base, "moomoo_my", "MYR") == Decimal("30000")

    _cash_deposit(page, base, ccy="MYR", amount="50000")     # -> 80,000 MYR
    _cash_deposit(page, base, ccy="USD", amount="10000")     # ->  10,000 USD
    _fx_convert(page, base, from_amt="20000", to_amt="4500")  # -20,000 MYR / +4,500 USD

    assert _cash_balance(base, "moomoo_my", "MYR") == Decimal("60000")   # 30k + 50k − 20k
    assert _cash_balance(base, "moomoo_my", "USD") == Decimal("14500")   # 10k + 4.5k

    assert not console_errors and not page_errors, (
        f"migrated-DB cash trio: console={console_errors!r} page={page_errors!r}"
    )


# =========================================================================================
# 5. Legacy-CSV alias (browser): a moomoo_my_us row previews 已合併 and commits onto moomoo_my
# =========================================================================================

@pytest.mark.e2e
def test_legacy_csv_account_alias_preview_and_commit(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """Pasting a CSV row carrying the legacy ``moomoo_my_us`` id previews the soft 已合併 alias
    notice (row resolved to moomoo_my) and commits the row onto the merged account."""
    base = flow_server(_seed_golden)  # AAPL (US) registered; moomoo_my merged
    page = fresh_page
    console_errors, page_errors = _sink(page)

    assert _holding(base, "AAPL", "moomoo_my") is None, "moomoo_my must start without AAPL"

    page.goto(base + "/trades.html", wait_until="load")
    page.wait_for_selector("#csv-kinds .chip", state="attached")
    page.click("#tab-csv")
    page.wait_for_selector("#csv-dropzone", state="visible")

    paste = (
        "account,symbol,side,date,shares,price\r\n"
        f"moomoo_my_us,AAPL,buy,{_BUY_DATE},10,100"
    )
    with page.expect_response("**/api/import/preview") as pv:
        page.fill("#csv-paste", paste)
    assert pv.value.status == 200
    page.wait_for_selector("#csv-body tr")
    # the alias notice surfaces in the preview row + the row already reads the resolved id.
    assert "已合併為 moomoo_my" in page.inner_text("#csv-body"), page.inner_text("#csv-body")
    assert "moomoo_my" in page.inner_text("#csv-body")

    # commit: the soft alias is a warn -> the server 422s once (warnings_unacknowledged), the
    # frontend raises the ack dialog; confirming re-commits with ack_warnings and lands the row.
    page.click("#csv-confirm")
    page.wait_for_selector(".modal-backdrop .modal-foot .btn-primary")
    page.click(".modal-backdrop .modal-foot .btn-primary")
    page.wait_for_selector("#csv-result", state="visible")

    landed = _holding(base, "AAPL", "moomoo_my")
    assert landed is not None and Decimal(landed["shares"]) == Decimal("10"), (
        "the aliased row must land on moomoo_my"
    )

    # the first commit's 422 is intrinsic to the ack flow -> the browser logs one benign
    # resource-load line for it; every OTHER console error (and any page error) must be absent.
    def _benign(t: str) -> bool:
        return ("Failed to load resource" in t) or ("422" in t) or ("import/commit" in t)
    unexpected = [t for t in console_errors if not _benign(t)]
    assert not unexpected and not page_errors, (
        f"legacy CSV alias: unexpected console={unexpected!r} page={page_errors!r}"
    )


# =========================================================================================
# 6. FX form boots ENABLED on the merged default + the single-ccy gate cannot leak back
# =========================================================================================

def _seed_fx_form(conn: Any) -> None:
    _seed_golden(conn)
    insert_cash_movement(conn, account_id="moomoo_my", move_date=date(2026, 1, 5),
                         kind="DEPOSIT", ccy=Currency.MYR, amount=Decimal("50000"))
    conn.commit()


@pytest.mark.e2e
def test_fx_form_boots_enabled_on_merged_and_single_ccy_gate_holds(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """The default-first account (ORDER BY account_id → moomoo_my) boots the FX form ENABLED
    (dual USD/MYR); tw_broker (TWD-only) disables the whole form, and a forced re-boot while it
    stays selected can never leak #cfx-confirm back to enabled (the Batch-A ordering guard)."""
    base = flow_server(_seed_fx_form)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    page.goto(base + "/cash.html#fx", wait_until="load")
    page.wait_for_selector("#cfx-account option", state="attached")
    page.wait_for_load_state("networkidle")

    # ---- boot-enabled on the merged default (no select_option) ---------------------------
    assert page.input_value("#cfx-account") == "moomoo_my", "moomoo_my is the default first"
    assert not page.is_disabled("#cfx-from-ccy"), "dual-ccy account boots the FX form enabled"
    assert page.is_hidden("#cfx-single-ccy")

    # ---- tw_broker (single-ccy TWD) -> the whole form disables + message -----------------
    page.select_option("#cfx-account", "tw_broker")
    page.wait_for_function("() => document.querySelector('#cfx-from-ccy').disabled === true")
    for sel in ("#cfx-from-ccy", "#cfx-to-ccy", "#cfx-from-amt", "#cfx-to-amt", "#cfx-confirm"):
        assert page.is_disabled(sel), f"{sel} must be disabled for a single-ccy account"
    assert page.is_visible("#cfx-single-ccy")

    # ---- ordering-leak regression: a fresh re-boot (tab re-dispatch) while tw_broker stays
    # selected must NOT re-enable #cfx-confirm via updCeiling()'s unconditional assign -------
    page.click('.cash-tab[data-tab="fx"]')
    page.wait_for_load_state("networkidle")
    assert page.input_value("#cfx-account") == "tw_broker"
    for sel in ("#cfx-from-ccy", "#cfx-to-ccy", "#cfx-from-amt", "#cfx-to-amt", "#cfx-confirm"):
        assert page.is_disabled(sel), f"{sel} must stay disabled after a re-boot while selected"

    # ---- back to the merged dual-ccy account -> fully re-enabled + message hidden ---------
    page.select_option("#cfx-account", "moomoo_my")
    page.wait_for_function("() => !document.querySelector('#cfx-from-ccy').disabled")
    for sel in ("#cfx-from-ccy", "#cfx-to-ccy", "#cfx-from-amt", "#cfx-to-amt", "#cfx-confirm"):
        assert not page.is_disabled(sel), f"{sel} must re-enable for a two-ccy account"
    assert page.is_hidden("#cfx-single-ccy")

    assert not console_errors and not page_errors, (
        f"fx boot/single-ccy gate: console={console_errors!r} page={page_errors!r}"
    )


# =========================================================================================
# 7. §8.3 both-order pins — soft ack-able overdraft vs hard no-ack fx block
# =========================================================================================

def _seed_thin_myr(conn: Any) -> None:
    """moomoo_my with a small MYR pool (10,000) + a registered MY counter, so ordered flows can
    drive the pool low and cross the two DISTINCT guards."""
    _seed_golden(conn)
    _reg_my(conn)
    insert_cash_movement(conn, account_id="moomoo_my", move_date=date(2026, 1, 5),
                         kind="DEPOSIT", ccy=Currency.MYR, amount=Decimal("10000"))
    conn.commit()


@pytest.mark.e2e
def test_convert_then_overdraft_buy_is_ackable_soft_gate(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """§8.3(a): drain MYR via a conversion first, then an MY buy that exceeds the remaining MYR
    raises the ACK-able cash_overdraft soft gate — #m-confirm is held until #m-ack, then commits."""
    base = flow_server(_seed_thin_myr)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    # convert most of the MYR away (9,000 of 10,000) -> ~1,000 MYR remains.
    _fx_convert(page, base, from_amt="9000", to_amt="2000")
    assert _cash_balance(base, "moomoo_my", "MYR") == Decimal("1000")

    # an MY buy costing ~9,018 MYR >> the 1,000 remaining -> the soft cash_overdraft gate.
    _open_manual(page, base)
    _manual_preview(page, account="moomoo_my", symbol="1155", shares="1000", price="9")
    page.wait_for_selector("#m-ack")                       # soft warn -> ack checkbox rendered
    assert page.is_disabled("#m-confirm"), "confirm gated until the overdraft is acknowledged"

    page.check("#m-ack")
    page.wait_for_function(
        "() => { const b = document.querySelector('#m-confirm'); return b && !b.disabled; }")
    _commit_manual(page)                                    # ack -> the write is permitted (201)

    held = _holding(base, "1155", "moomoo_my")
    assert held is not None and Decimal(held["shares"]) == Decimal("1000")

    assert not console_errors and not page_errors, (
        f"convert-then-overdraft ack gate: console={console_errors!r} page={page_errors!r}"
    )


@pytest.mark.e2e
def test_buy_then_overdraft_conversion_is_hard_no_ack_block(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    """§8.3(b): drain MYR via an MY buy first, then an over-pool MYR→USD conversion is a HARD
    block — the inline balance error shows and #cfx-confirm stays disabled with NO ack path."""
    base = flow_server(_seed_thin_myr)
    page = fresh_page
    console_errors, page_errors = _sink(page)

    # a clean MY buy (~9,018 MYR of 10,000) leaves the pool positive but thin (no overdraft).
    _open_manual(page, base)
    _manual_preview(page, account="moomoo_my", symbol="1155", shares="1000", price="9")
    page.wait_for_function(
        "() => { const b = document.querySelector('#m-confirm'); return b && !b.disabled; }")
    assert page.query_selector("#m-ack") is None, "a covered buy must NOT raise the ack gate"
    _commit_manual(page)

    remaining = _cash_balance(base, "moomoo_my", "MYR")
    assert remaining is not None and Decimal("0") < remaining < Decimal("2000"), remaining

    # now a MYR→USD conversion for MORE than the thin remaining pool -> hard guard, no ack.
    page.goto(base + "/cash.html#fx", wait_until="load")
    page.wait_for_selector("#cfx-account option", state="attached")
    page.select_option("#cfx-account", "moomoo_my")
    page.wait_for_function(
        "() => { const n = document.querySelector('#cfx-balance');"
        " return n && n.textContent.includes('可用餘額') && n.textContent.includes('MYR'); }")
    page.fill("#cfx-from-amt", "5000")   # >> the thin remaining MYR pool
    page.fill("#cfx-to-amt", "1100")
    page.wait_for_selector("#cfx-amt-err", state="visible")
    assert page.is_disabled("#cfx-confirm"), "over-pool conversion is a HARD block (no ack)"
    # the FX form has NO acknowledgement affordance (unlike the soft buy-overdraft gate).
    assert page.query_selector("#cfx-ack") is None
    # the pool is unchanged — nothing was written.
    assert _cash_balance(base, "moomoo_my", "MYR") == remaining

    assert not console_errors and not page_errors, (
        f"buy-then-conversion hard block: console={console_errors!r} page={page_errors!r}"
    )
