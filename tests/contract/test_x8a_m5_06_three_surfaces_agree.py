"""X8a — M5-06's tail: the two readers that still read the WHOLE history.

X1 made ``GET /api/cash`` and ``GET /api/cash/statement`` date-aware (owner ruling
2026-09-06, option C: a balance is as of the app clock's day; a row dated after it is in the
ledger, not in today's balance). Two more surfaces quote 「今天有多少現金」 and were left
reading the whole history, so the original finding — two screens, two answers for today's
cash — had only moved house:

* the manual-entry draft's 賬戶現金 line (``input_center._account_cash``), whose own
  docstring promised 「the SAME cash_balances figure /api/cash serves」, and the soft overdraft
  warning beside it (``_cash_overdraft_issue``), which a far-future deposit could silence;
* the printed 現金收支明細's 「目前餘額」 (``export/cash_statement._pool_section``), which
  printed the running balance after the LAST row — a projection whenever a future row exists.

The assertion here is the sentence, not the parameter: after a far-future deposit the 資金
page, the draft line and the printed statement must quote ONE figure for today's cash. The
counter-evidence is the golden ledger untouched (every row dated before GOLDEN_NOW): all three
surfaces stay digit-for-digit what they were, and the overdraft warning keeps its verdicts.

"Today" is ``api.deps.get_now`` → ``shared.clock.app_now`` (Asia/Taipei), frozen at
GOLDEN_NOW (2026-06-11) here — never ``date.today()``. The print path reads the ``now`` its
header already received, never a second clock read (which would split the header's date from
the balance's on an export that crosses midnight).
"""

import re
import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from portfolio_dash.export import cash_statement
from portfolio_dash.export.cash_statement import build_cash_statement_report_html
from portfolio_dash.export.report_html import _fmt_amount
from portfolio_dash.shared.enums import Currency
from tests.conftest import GOLDEN_NOW

_TODAY = "2026-06-11"  # GOLDEN_NOW's date
_GOLDEN_TW_TWD = "-495000"  # tw_broker TWD after the golden 1000 × 500 TSMC settlement

_FAR_FUTURE_DEPOSIT = {
    "account_id": "tw_broker", "date": "2099-01-01", "kind": "deposit",
    "ccy": "TWD", "amount": "777777", "note": "X8a far-future deposit",
}
_BUY_2330 = {
    "account_id": "tw_broker", "symbol": "2330", "side": "buy",
    "date": _TODAY, "shares": "1000", "price": "600",
}

# 「TWD 資金池 · 目前餘額 -495,000 TWD」 → the formatted amount between the label and the ccy.
_TITLE_RE = re.compile(r"<h2>(?P<ccy>[A-Z]{3}) 資金池 · 目前餘額 (?P<amount>[-0-9,.]+) (?P=ccy)")


# --- the three surfaces ------------------------------------------------------------------


def _api_cash_pool(client: TestClient, account: str, ccy: str) -> str:
    body = client.get("/api/cash", params={"limit": 500}).json()
    return str(next(b["amount"] for b in body["balances"]
                    if b["account_id"] == account and b["ccy"] == ccy))


def _draft_line(client: TestClient, body: dict[str, Any] = _BUY_2330) -> dict[str, Any]:
    r = client.post("/api/input/manual/preview", json=body)
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def _print_titles(doc: str) -> dict[str, str]:
    """Per-pool 目前餘額 as the printed string (thousands separators, minor-unit dp)."""
    return {m.group("ccy"): m.group("amount") for m in _TITLE_RE.finditer(doc)}


def _print_report(client: TestClient, account: str, ccy: str | None = None) -> str:
    body: dict[str, Any] = {"account": account}
    if ccy is not None:
        body["ccy"] = ccy
    r = client.post("/api/export/cash-statement-report", json=body)
    assert r.status_code == 200, r.text
    return str(r.content.decode("utf-8"))


def _as_printed(api_amount: str, ccy: str) -> str:
    """The API's Decimal string pushed through the report's OWN formatter — the print shows
    thousands separators at the minor unit (``-495000`` → ``-495,000``, ``0`` → ``0.00``), so
    equality is asserted on the formatted string, never on a re-parsed number."""
    return _fmt_amount(Decimal(api_amount), ccy)


# --- the finding: two more readers, one sentence -----------------------------------------


def test_draft_account_cash_line_is_todays_balance(api_client: TestClient) -> None:
    """The manual-entry draft's 賬戶現金 line quotes the 資金 page's figure — as of today —
    after a 2099 deposit, not the whole-history sum."""
    assert api_client.post("/api/cash/movements", json=_FAR_FUTURE_DEPOSIT).status_code == 201
    api_cash = _api_cash_pool(api_client, "tw_broker", "TWD")
    assert api_cash == _GOLDEN_TW_TWD
    draft = _draft_line(api_client)
    assert draft["account_cash"] == {"ccy": "TWD", "balance": api_cash}
    # cash_after is balance + the signed total, off the SAME balance (no second engine pass).
    assert Decimal(draft["cash_after"]) == Decimal(api_cash) + Decimal(draft["total"])


def test_printed_statement_current_balance_is_todays_balance(api_client: TestClient) -> None:
    """The printed 現金收支明細's 「目前餘額」 is today's balance, not the running balance after
    the last (future) row."""
    assert api_client.post("/api/cash/movements", json=_FAR_FUTURE_DEPOSIT).status_code == 201
    api_cash = _api_cash_pool(api_client, "tw_broker", "TWD")
    titles = _print_titles(_print_report(api_client, "tw_broker", "TWD"))
    assert titles["TWD"] == _as_printed(api_cash, "TWD"), titles


def test_three_surfaces_quote_one_figure_after_a_far_future_deposit(
    api_client: TestClient,
) -> None:
    """The sentence itself: 資金 page, draft line, printed statement — one number."""
    assert api_client.post("/api/cash/movements", json=_FAR_FUTURE_DEPOSIT).status_code == 201
    api_cash = _api_cash_pool(api_client, "tw_broker", "TWD")
    draft = _draft_line(api_client)["account_cash"]["balance"]
    printed = _print_titles(_print_report(api_client, "tw_broker"))["TWD"]
    assert api_cash == draft == _GOLDEN_TW_TWD, (api_cash, draft, printed)
    assert printed == _as_printed(api_cash, "TWD") == "-495,000", (api_cash, draft, printed)


# --- counter-evidence: a ledger with no future row does not move a digit ----------------


def test_golden_ledger_without_a_future_row_is_unchanged_on_all_three_surfaces(
    api_client: TestClient,
) -> None:
    """Every golden row is dated before GOLDEN_NOW, so the bound is a no-op: the three
    surfaces reproduce the recorded balances digit for digit, and the printed statement
    carries no future cut at all."""
    api_cash = _api_cash_pool(api_client, "tw_broker", "TWD")
    assert api_cash == _GOLDEN_TW_TWD
    assert _draft_line(api_client)["account_cash"] == {"ccy": "TWD", "balance": api_cash}
    doc = _print_report(api_client, "tw_broker")
    assert _print_titles(doc)["TWD"] == _as_printed(api_cash, "TWD")
    assert "未來日期" not in doc and "截至" not in doc and "future" not in doc
    # The other account's pools, both currencies, the same way.
    schwab = _print_titles(_print_report(api_client, "schwab"))
    assert schwab["USD"] == _as_printed(_api_cash_pool(api_client, "schwab", "USD"), "USD")
    assert schwab["TWD"] == _as_printed(_api_cash_pool(api_client, "schwab", "TWD"), "TWD")


# --- the overdraft warning: a future deposit can no longer silence it -------------------


def test_a_future_deposit_no_longer_silences_the_overdraft_warning(
    api_client: TestClient,
) -> None:
    """tw_broker TWD sits at −495,000. A 2099 deposit of 100,000,000 makes the account
    "tracked" and, read over the whole history, would cover any buy — so the soft warning
    went silent on a pool that is negative TODAY."""
    assert api_client.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": "2099-01-01", "kind": "deposit",
        "ccy": "TWD", "amount": "100000000"}).status_code == 201
    codes = {i["code"] for i in _draft_line(api_client)["issues"]}
    assert "cash_overdraft" in codes, codes


def test_a_deposit_dated_on_or_before_today_still_covers_the_buy(
    api_client: TestClient,
) -> None:
    """Counter-evidence, both directions of the bound: a deposit dated ON today (inclusive)
    or earlier is in today's balance, so a covered buy is still not warned about."""
    for on in (_TODAY, "2026-01-01"):
        assert api_client.post("/api/cash/movements", json={
            "account_id": "tw_broker", "date": on, "kind": "deposit",
            "ccy": "TWD", "amount": "100000000"}).status_code == 201
        codes = {i["code"] for i in _draft_line(api_client)["issues"]}
        assert "cash_overdraft" not in codes, (on, codes)


def test_a_tracked_but_uncovered_buy_is_still_warned(api_client: TestClient) -> None:
    """The verdict the warning exists for, unchanged: tracked (one small deposit) and the
    pool cannot fund the buy today → the soft issue fires."""
    assert api_client.post("/api/cash/movements", json={
        "account_id": "tw_broker", "date": "2026-01-01", "kind": "deposit",
        "ccy": "TWD", "amount": "1000"}).status_code == 201
    codes = {i["code"] for i in _draft_line(api_client)["issues"]}
    assert "cash_overdraft" in codes, codes


# --- the printed statement: every row listed, the cut marked, the projection labelled ----


def test_printed_statement_lists_the_future_row_after_a_marked_cut(
    api_client: TestClient,
) -> None:
    """Print is a static page: no hover, no paging. It says what the web statement says —
    every row is listed, the balance is as of today, and the rows past it are marked — with
    the cut drawn once on the page and counted in the title."""
    assert api_client.post("/api/cash/movements", json=_FAR_FUTURE_DEPOSIT).status_code == 201
    doc = _print_report(api_client, "tw_broker", "TWD")
    assert f"目前餘額 -495,000 TWD（截至 {_TODAY}，不含 1 筆未來日期）" in doc
    # The future row is still printed, with its running balance — a labelled projection.
    assert "2099-01-01" in doc and "282,777" in doc
    cut = doc.index("以下 1 筆為未來日期")
    assert doc.index("2099-01-01") > cut > doc.index("2026-01-05")
    assert '<tr class="future">' in doc


def test_printed_statement_empty_pool_is_unchanged(api_client: TestClient) -> None:
    doc = _print_report(api_client, "schwab", "MYR")
    assert "本帳戶此幣別尚無紀錄" in doc
    assert _print_titles(doc)["MYR"] == "0.00"


# --- whose "today": the header's now, never the wall clock -------------------------------


def test_print_balance_follows_the_injected_now_not_the_wall_clock(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """The builder is handed ``now`` for its header; the balance must be as of THAT day. Moving
    the injected clock past the 2099 row folds it into 目前餘額 and drops the cut — so the
    figure follows the parameter, not the machine's date."""
    assert api_client.post("/api/cash/movements", json=_FAR_FUTURE_DEPOSIT).status_code == 201

    def _titles(now: datetime) -> tuple[dict[str, str], str]:
        art = build_cash_statement_report_html(
            golden_db, account="tw_broker", ccy=Currency.TWD, now=now)
        assert art is not None
        doc = art.content.decode("utf-8")
        return _print_titles(doc), doc

    frozen, doc = _titles(GOLDEN_NOW)
    assert frozen["TWD"] == "-495,000" and "不含 1 筆未來日期" in doc
    later, doc = _titles(datetime(2099, 1, 2, 9, 0, tzinfo=ZoneInfo("Asia/Taipei")))
    assert later["TWD"] == "282,777" and "未來日期" not in doc
    # Inclusive on the bound: dated ON the clock's day counts.
    on_the_day, doc = _titles(datetime(2099, 1, 1, 0, 0, tzinfo=ZoneInfo("Asia/Taipei")))
    assert on_the_day["TWD"] == "282,777" and "未來日期" not in doc


def test_print_path_never_reads_its_own_clock() -> None:
    """A second clock read on the print path would date the balance differently from the
    header on an export that crosses midnight; the module must not own a clock."""
    import inspect

    src = inspect.getsource(cash_statement)
    assert "date.today" not in src and "app_now" not in src and "datetime.now" not in src
