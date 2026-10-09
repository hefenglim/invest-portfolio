"""DEF-056 addendum (owner ruling 2026-09-24: every ledger row counts from its own date): the
two surfaces the class fix left — the 公司行動 list wire and the printed 帳本報告 — say
「未來日期：YYYY-MM-DD 起計入」 for a row dated after the server's valuation day, through the
SAME predicate the valuation cut reads (``shared.models.ledger.pending_from``).

The valuation already leaves a future-dated corporate action out (agent P's cut), and the
five other ledger lists already flag their rows; the 公司行動 tab and the printed report did
not, so a printed ledger listed a row the totals beside it do not reflect with no word why.
Pinned on the rendered payload / HTML, never on the builder's source.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import (
    insert_cash_movement,
    insert_corporate_action,
    insert_dividend,
    insert_fx_conversion,
    insert_transaction,
    upsert_opening,
)
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import Side

D = Decimal
#: GOLDEN_NOW is 2026-06-11 (Asia/Taipei) — the valuation day of every request here.
_AHEAD = date(2026, 7, 1)
_PAST = date(2026, 6, 1)
_MARK = "未來日期：2026-07-01 起計入"


def _action(conn: sqlite3.Connection, on: date) -> int:
    action = insert_corporate_action(
        conn, account_id="tw_broker", action_date=on, kind=CorporateActionKind.SPLIT,
        from_symbol="2330", to_symbol="2330", ratio_to=D("2"), ratio_from=D("1"))
    conn.commit()
    return action


def test_the_action_list_flags_a_future_row_and_only_it(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    ahead = _action(golden_db, _AHEAD)
    today = _action(golden_db, date(2026, 6, 11))
    past = _action(golden_db, _PAST)
    rows = {r["id"]: r for r in api_client.get(
        "/api/ledgers/corporate-actions", params={"limit": 500}).json()["rows"]}
    assert rows[ahead]["counts_from"] == "2026-07-01"
    assert rows[today]["counts_from"] is None          # a row dated today has happened
    assert rows[past]["counts_from"] is None


def _seed_every_ledger(conn: sqlite3.Connection) -> None:
    """One future row in each of the six printed ledgers, plus the rows that must NOT flag."""
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=D("10"), price=D("600"), fees=D("20"), tax=D("0"),
                       trade_date=_AHEAD)
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=_AHEAD,
                    div_type="CASH", gross=D("5000"), withholding=D("0"), net=D("5000"))
    # A 配股 PAID after today but ex-dated before it counts from its ex-date (the valuation
    # cut's own rule, ``dividend_effective_date``) — so it must NOT be flagged.
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=date(2026, 8, 1),
                    ex_date=_PAST, div_type="STOCK", gross=D("0"), withholding=D("0"),
                    net=D("0"), reinvest_shares=D("50"))
    insert_fx_conversion(conn, account_id="schwab", date=_AHEAD, from_ccy=Currency.TWD,
                         from_amount=D("33500"), to_ccy=Currency.USD, to_amount=D("1000"))
    upsert_opening(conn, account_id="schwab", symbol="AAPL", shares=D("5"),
                   original_cost_total=D("500"), build_date=_AHEAD)
    insert_cash_movement(conn, account_id="schwab", move_date=_AHEAD, kind="DEPOSIT",
                         ccy=Currency.USD, amount=D("100"))
    _action(conn, _AHEAD)
    conn.commit()


def _section(doc: str, title: str) -> str:
    start = doc.index(f">{title}<")
    nxt = doc.find("<h2>", start + 1)
    return doc[start:nxt if nxt != -1 else len(doc)]


def _rows(section: str) -> list[str]:
    return re.findall(r"<tr>(.*?)</tr>", section, re.S)


def test_every_printed_ledger_flags_its_future_rows(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_every_ledger(golden_db)
    doc = api_client.post("/api/export/ledgers-report", json={}).content.decode("utf-8")
    for title in ("交易紀錄", "股利紀錄", "換匯紀錄", "期初庫存", "公司行動", "資金收支"):
        rows = _rows(_section(doc, title))
        flagged = [r for r in rows if _MARK in r]
        future = [r for r in rows if "2026-07-01" in r]
        assert flagged and flagged == future, (title, rows)
        # Every row dated on or before the valuation day carries no marker.
        assert all("未來日期" not in r for r in rows if r not in future), (title, rows)


def test_a_stock_dividend_counts_from_its_ex_date_in_print_as_in_the_list(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """One date rule: the printed marker and the ledger list's ``counts_from`` agree on a
    配股 paid after today but ex-dated before it (neither flags it)."""
    _seed_every_ledger(golden_db)
    listed = [d for d in api_client.get(
        "/api/ledgers/dividends", params={"limit": 500}).json()["rows"]
        if d["date"] == "2026-08-01"]
    assert listed and listed[0]["counts_from"] is None
    doc = api_client.post("/api/export/ledgers-report", json={}).content.decode("utf-8")
    stock = [r for r in _rows(_section(doc, "股利紀錄")) if "2026-08-01" in r]
    assert stock and "未來日期" not in stock[0]
