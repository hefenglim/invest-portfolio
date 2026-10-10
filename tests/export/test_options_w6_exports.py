"""Options W6 — every export door on an OPTION ledger (spec §9).

The ledger is ``tests/export/options_w6_seed.py`` (synthetic): 10 AAPL, an open sold put, a
closed call and an expired put. Stock-ledger byte identity is the sibling module
``test_options_w6_stock_exports_unchanged.py``; this one pins what an option ledger adds.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
import zipfile
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_transaction,
    list_corporate_actions,
    list_instruments,
    list_transactions,
)
from portfolio_dash.export import holdings as holdings_mod
from portfolio_dash.export.cash_statement import (
    build_cash_statement_csv,
    build_cash_statement_report_html,
)
from portfolio_dash.export.holdings import (
    OPTION_COLUMNS,
    build_holdings_csv,
    build_options_csv,
)
from portfolio_dash.export.holdings_report import build_holdings_report_html
from portfolio_dash.export.ledgers_report import build_ledgers_report_html
from portfolio_dash.export.realized import build_realized_csv
from portfolio_dash.export.rebalance_report import build_rebalance_report_html
from portfolio_dash.export.symbol_detail import build_symbol_detail_csv
from portfolio_dash.export.tax import build_tax_package_zip
from portfolio_dash.portfolio.cash import option_sides_of
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.options import REFUSED_CLOSE_REMEDY, OptionSide
from portfolio_dash.shared.wire import decimal_str
from tests.conftest import GOLDEN_NOW, _seed_golden, init_golden_base
from tests.export.options_w6_seed import CALL, EXP, NOW, PUT, option_ledger

D = Decimal
TWD = Currency.TWD
REFUSED = "JKL260515P00010000"
ADJ_FROM = "QRS261218P00060000"
ADJ_TO = "QRS261218P00020000"


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = option_ledger()
    try:
        yield c
    finally:
        c.close()


def _add_refused_close(conn: sqlite3.Connection) -> None:
    """A sold put whose ``expired`` row covers twice the short — the replay refuses it (§4.4)
    and the 1-contract short survives (待釐清). Raw rows: no door would write this."""
    register_option_contract(conn, REFUSED)
    insert_transaction(conn, account_id="schwab", symbol=REFUSED, side=Side.SELL,
                       quantity=D("100"), price=D("0.50"), fees=D("0.65"), tax=D("0"),
                       trade_date=date(2026, 3, 2), short_sale=True)
    insert_transaction(conn, account_id="schwab", symbol=REFUSED, side=Side.BUY,
                       quantity=D("200"), price=D("0"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 5, 15), close_reason="expired")
    conn.commit()


def _csv_rows(content: bytes) -> list[list[str]]:
    text = content.decode("utf-8-sig")
    return [r for r in csv.reader(io.StringIO(text)) if not (r and r[0].startswith("#"))]


def _plain(content: bytes) -> str:
    """An HTML report's text, tags stripped (the stylesheet dropped)."""
    html = content.decode("utf-8").split("</style>", 1)[1]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# --- 已實現損益 CSV -------------------------------------------------------------------------


def test_the_realized_csv_lists_the_option_rows_after_the_stock_rows(
    conn: sqlite3.Connection,
) -> None:
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.SELL,
                       quantity=D("2"), price=D("110"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 5, 1))
    conn.commit()
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    assert data.options is not None
    rows = _csv_rows(build_realized_csv(conn, now=NOW, reporting=TWD).content)
    header, body = rows[0], rows[1:]
    assert header[-2:] == ["asset_type", "contract"]
    assert [(r[1], r[3], r[-2], r[-1]) for r in body] == [
        ("AAPL", "sale", "stock", ""),
        (CALL, "sale", "option", "XYZ 2026-05-15 60 Call"),
        (EXP, "short_cover", "option", "ABC 2026-04-17 30 Put"),
    ]
    # The cells are the replay's own rows (the block's), at source precision.
    for r, src in zip(body[1:], data.options.realized.rows, strict=True):
        assert (r[6], r[9]) == (decimal_str(src.proceeds_net), decimal_str(src.realized))
    assert [r[9] for r in body[1:]] == ["48.69", "99.3500"]


# --- 持股 CSV / 選擇權持倉 CSV (owner ruling 2026-10-10: B, two files) ------------------------

LONG = "XYZ261120C00070000"
_SCI = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)[eE][+-]?\d+$")

#: The open sold put, every column (the §7.1 ``open[]`` keys + the reporting-currency value).
_PUT_ROW = {
    "account_id": "schwab", "symbol": PUT, "contract": "XYZ 2026-12-18 50 Put",
    "underlying": "XYZ", "expiry": "2026-12-18", "days_to_expiry": "191", "strike": "50",
    "right": "P", "direction": "short", "contracts": "2", "share_equivalents": "-200",
    "average_premium": "1.9935", "premium_total": "398.7000", "last_price": "2.00",
    "last_price_date": "2026-03-02", "market_value": "-400.00", "unrealized": "-1.3000",
    "obligation": "10000", "underlying_price": "", "underlying_price_date": "",
    "past_expiry": "False", "unbookable_close": "False", "oversold": "False",
    "reporting_ccy_value": "-12800.00"}
#: A bought call, open: no obligation, a positive value.
_CALL_ROW = {
    "account_id": "schwab", "symbol": LONG, "contract": "XYZ 2026-11-20 70 Call",
    "underlying": "XYZ", "expiry": "2026-11-20", "days_to_expiry": "163", "strike": "70",
    "right": "C", "direction": "long", "contracts": "1", "share_equivalents": "100",
    "average_premium": "1.2065", "premium_total": "120.65", "last_price": "1.20",
    "last_price_date": "2026-03-10", "market_value": "120.00", "unrealized": "-0.6500",
    "obligation": "", "underlying_price": "", "underlying_price_date": "",
    "past_expiry": "False", "unbookable_close": "False", "oversold": "False",
    "reporting_ccy_value": "3840.00"}


def _add_long_call(conn: sqlite3.Connection) -> None:
    register_option_contract(conn, LONG)
    insert_transaction(conn, account_id="schwab", symbol=LONG, side=Side.BUY,
                       quantity=D("100"), price=D("1.20"), fees=D("0.65"), tax=D("0"),
                       trade_date=date(2026, 3, 10))
    conn.commit()


def _footer(content: bytes) -> list[str]:
    text = content.decode("utf-8-sig")
    return [line for line in text.split("\r\n") if line.startswith("#")]


def test_the_holdings_csv_is_the_stock_table_and_the_option_line(
    conn: sqlite3.Connection,
) -> None:
    """B: the holdings file is the stock table ONLY — no blank row, no 「選擇權」 title, no
    option header, no contract. The footer line naming the options' part of the KPI stays: it
    is why the KPI 總市值 differs from the sum of the stock rows."""
    art = build_holdings_csv(conn, now=NOW, reporting=TWD)
    assert art.filename == "holdings_snapshot_2026-06-10.csv"
    rows = _csv_rows(art.content)
    assert [r[0] for r in rows[1:]] == ["AAPL"]
    assert all(len(r) == len(rows[0]) for r in rows)
    text = art.content.decode("utf-8-sig")
    assert "選擇權" not in text and PUT not in text and OPTION_COLUMNS not in rows
    assert _footer(art.content)[1:] == [
        "# total_market_value=25600.00, options_market_value=-12800.00, reporting_ccy=TWD"]


@pytest.mark.parametrize("kwargs", [{"account": "tw_broker"}, {"market": Market.TW}])
def test_a_filtered_holdings_csv_keeps_the_option_line_and_no_block(
    conn: sqlite3.Connection, kwargs: dict[str, object],
) -> None:
    art = build_holdings_csv(conn, now=NOW, reporting=TWD, **kwargs)  # type: ignore[arg-type]
    assert _csv_rows(art.content)[1:] == []
    footer = _footer(art.content)
    assert footer[1].startswith("# total_market_value=25600.00, options_market_value=")
    assert footer[2].startswith("# filter: ")


def test_the_options_csv_lists_every_open_contract(conn: sqlite3.Connection) -> None:
    _add_long_call(conn)
    art = build_options_csv(conn, now=NOW, reporting=TWD)
    assert art.filename == "options_snapshot_2026-06-10.csv"
    assert art.media_type == "text/csv; charset=utf-8"
    rows = _csv_rows(art.content)
    assert rows[0] == OPTION_COLUMNS
    # The block's own order (剩餘天數 ↑): the call expires first.
    assert [dict(zip(OPTION_COLUMNS, r, strict=True)) for r in rows[1:]] == [
        _CALL_ROW, _PUT_ROW]
    # The cells are the block's own values at source precision (the export computes nothing).
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    assert data.options is not None
    for r, src in zip(rows[1:], data.options.open, strict=True):
        assert src.market_value is not None and src.unrealized is not None
        assert (r[15], r[16]) == (decimal_str(src.market_value), decimal_str(src.unrealized))
    # The footer convention is the holdings CSV's: as_of, the rates, the generation stamp.
    holdings = build_holdings_csv(conn, now=NOW, reporting=TWD)
    assert _footer(art.content) == _footer(holdings.content)[:1]
    assert _footer(art.content)[0].startswith("# as_of=2026-06-10, fx_rates={USD:32")


@pytest.mark.parametrize("kwargs, suffix, symbols", [
    ({"account": "tw_broker"}, "_tw_broker_all", []),
    ({"market": Market.TW}, "_all_TW", []),
    ({"account": "schwab"}, "_schwab_all", [LONG, PUT]),
    ({"market": Market.US}, "_all_US", [LONG, PUT]),
    ({"account": "schwab", "market": Market.US}, "_schwab_US", [LONG, PUT]),
])
def test_the_options_csv_follows_the_holdings_filter(
    conn: sqlite3.Connection, kwargs: dict[str, object], suffix: str, symbols: list[str],
) -> None:
    """The same (account, market) chips as the holdings CSV, recorded the same way: in the
    filename and in a ``filter:`` footer line."""
    _add_long_call(conn)
    art = build_options_csv(conn, now=NOW, reporting=TWD, **kwargs)  # type: ignore[arg-type]
    assert art.filename == f"options_snapshot_2026-06-10{suffix}.csv"
    rows = _csv_rows(art.content)
    assert rows[0] == OPTION_COLUMNS and [r[1] for r in rows[1:]] == symbols
    holdings = build_holdings_csv(conn, now=NOW, reporting=TWD,
                                  **kwargs)  # type: ignore[arg-type]
    assert holdings.filename == f"holdings_snapshot_2026-06-10{suffix}.csv"
    assert _footer(art.content)[-1] == _footer(holdings.content)[-1]
    assert _footer(art.content)[-1].startswith("# filter: account=")


def test_a_stock_ledger_options_csv_is_the_header_alone() -> None:
    """A ledger with no option answers what every export answers when there is nothing to
    list: the header and the footer, no rows."""
    stock = sqlite3.connect(":memory:", check_same_thread=False)
    stock.row_factory = sqlite3.Row
    try:
        init_golden_base(stock)
        _seed_golden(stock)
        art = build_options_csv(stock, now=GOLDEN_NOW, reporting=TWD)
        assert art.filename == "options_snapshot_2026-06-11.csv"
        assert _csv_rows(art.content) == [OPTION_COLUMNS]
        assert [line.split(",", 1)[0] for line in _footer(art.content)] == [
            "# as_of=2026-06-11"]
    finally:
        stock.close()


def test_an_oversold_contract_is_listed_flagged_with_no_value(
    conn: sqlite3.Connection,
) -> None:
    """An undeclared oversell (賣超, 待釐清) does not take the file down: the row is listed with
    ``oversold=True`` and blank value cells — never a fabricated figure."""
    oversold = "XYZ261218C00090000"
    register_option_contract(conn, oversold)
    insert_transaction(conn, account_id="schwab", symbol=oversold, side=Side.SELL,
                       quantity=D("100"), price=D("1.00"), fees=D("0.65"), tax=D("0"),
                       trade_date=date(2026, 3, 12))
    conn.commit()
    rows = _csv_rows(build_options_csv(conn, now=NOW, reporting=TWD).content)
    by_sym = {r[1]: dict(zip(OPTION_COLUMNS, r, strict=True)) for r in rows[1:]}
    row = by_sym[oversold]
    assert (row["oversold"], row["market_value"], row["unrealized"],
            row["reporting_ccy_value"]) == ("True", "", "", "")
    assert by_sym[PUT] == _PUT_ROW


def test_the_options_csv_never_prints_scientific_notation(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every Decimal cell goes through ``decimal_str`` (``_s``): an exact zero inherited from
    a repeating average reads ``0.000…``, never ``0E-24``."""
    def _tiny(*args: Any, **kwargs: Any) -> Any:
        data = build_dashboard(*args, **kwargs)   # the real one, imported above
        assert data.options is not None
        row = data.options.open[0].model_copy(update={
            "unrealized": D("0E-24"), "average_premium": D("5E-7")})
        return data.model_copy(update={
            "options": data.options.model_copy(update={"open": [row]})})

    monkeypatch.setattr(holdings_mod, "build_dashboard", _tiny)
    rows = _csv_rows(build_options_csv(conn, now=NOW, reporting=TWD).content)
    put = dict(zip(OPTION_COLUMNS, rows[1], strict=True))
    assert (put["unrealized"], put["average_premium"]) == (
        "0.000000000000000000000000", "0.0000005")
    assert [c for r in rows for c in r if _SCI.match(c)] == []


# --- 持股報表 ------------------------------------------------------------------------------


def test_the_holdings_report_prints_the_option_section_and_lines(
    conn: sqlite3.Connection,
) -> None:
    text = _plain(build_holdings_report_html(conn, now=NOW, reporting=TWD).content)
    assert "總市值 25,600 TWD 其中選擇權 -12,800 TWD" in text
    assert "未實現損益 6,358 TWD 其中選擇權 -42 TWD" in text
    assert "已實現損益 4,737 TWD 其中選擇權 4,737 TWD" in text
    section = text.split(" 選擇權 未平倉 ", 1)[1]
    assert "XYZ 2026-12-18 50 Put 賣權 {account:schwab} 賣方 2 口 191 50.00 398.70" in section
    assert "履約義務合計：{account:schwab} 10,000.00 USD" in section
    assert "已實現（依合約）" in section
    assert "XYZ 2026-05-15 60 Call {account:schwab} 2026-03-03 2026-04-01 賣出平倉" in section
    assert "ABC 2026-04-17 30 Put {account:schwab} 2026-03-05 2026-04-17 到期作廢" in section
    assert "選擇權已實現合計：148.04 USD（4,737 TWD）" in section
    # The 持倉明細表 stays stock-only, and the section comes after it (§7.1).
    assert text.index("持倉明細表") < text.index(" 選擇權 未平倉 ") < text.index("產業配置")


def test_the_holdings_report_marks_a_refused_close_and_a_past_expiry(
    conn: sqlite3.Connection,
) -> None:
    _add_refused_close(conn)
    text = _plain(build_holdings_report_html(conn, now=NOW, reporting=TWD).content)
    assert "JKL 2026-05-15 10 Put 賣權 已過到期日・待登錄 未入帳（待釐清）" in text
    assert f"未入帳（待釐清）：2026-05-15・{{account:schwab}}・{REFUSED}" in text
    assert REFUSED_CLOSE_REMEDY in text


# --- 稅務包 --------------------------------------------------------------------------------


def test_the_tax_package_names_the_contract_and_subtotals_the_options(
    conn: sqlite3.Connection,
) -> None:
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.SELL,
                       quantity=D("2"), price=D("110"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 5, 1))
    conn.commit()
    art = build_tax_package_zip(conn, now=NOW, year=2026, reporting=TWD)
    with zipfile.ZipFile(io.BytesIO(art.content)) as zf:
        sheet = _csv_rows(zf.read("realized_gains_2026.csv"))
        summary = zf.read("summary.md").decode("utf-8")
    assert sheet[0][-2:] == ["asset_type", "contract"]
    # The replay's own row order (by date): the sheet lists every capital gain together.
    assert [(r[2], r[-2], r[-1]) for r in sheet[1:]] == [
        (CALL, "option", "XYZ 2026-05-15 60 Call"),
        (EXP, "option", "ABC 2026-04-17 30 Put"),
        ("AAPL", "stock", ""),
    ]
    filing = summary.split("## Realized gains", 1)[1].split("## Dividends", 1)[0]
    assert "- USD: 168.0400" in filing     # 20.00 AAPL + 148.04 options
    assert filing.split("其中選擇權", 1)[1].splitlines()[1] == "- USD: 148.0400"


# --- 帳本報告 ------------------------------------------------------------------------------


def test_the_ledgers_report_prints_contracts_and_side_chips(conn: sqlite3.Connection) -> None:
    text = _plain(build_ledgers_report_html(conn, now=NOW, frm=None, to=None).content)
    assert f"{PUT} XYZ 2026-12-18 50 Put 賣出開倉 200（2 口） 2.00 1.30" in text
    assert f"{CALL} XYZ 2026-05-15 60 Call 買進開倉 100（1 口）" in text
    assert f"{CALL} XYZ 2026-05-15 60 Call 賣出平倉 100（1 口）" in text
    assert f"{EXP} ABC 2026-04-17 30 Put 到期作廢 100（1 口） 0.00" in text
    # A stock row keeps its bare 買 chip and share count.
    assert "AAPL Apple 買 10 100.00" in text


def test_the_ledgers_report_marks_a_refused_close(conn: sqlite3.Connection) -> None:
    _add_refused_close(conn)
    html = build_ledgers_report_html(conn, now=NOW, frm=None, to=None).content.decode()
    assert '<tr class="unbooked">' in html
    text = _plain(html.encode())
    assert f"{REFUSED} JKL 2026-05-15 10 Put 未入帳（待釐清） 200（2 口）" in text
    assert f"2026-05-15・{REFUSED}・未入帳（待釐清）：{REFUSED_CLOSE_REMEDY}" in text


def test_the_ledgers_report_lists_an_option_adjustment(conn: sqlite3.Connection) -> None:
    for sym in (ADJ_FROM, ADJ_TO):
        register_option_contract(conn, sym)
    insert_transaction(conn, account_id="schwab", symbol=ADJ_FROM, side=Side.SELL,
                       quantity=D("100"), price=D("3.00"), fees=D("0.65"), tax=D("0"),
                       trade_date=date(2026, 3, 2), short_sale=True)
    insert_corporate_action(conn, account_id="schwab", action_date=date(2026, 4, 1),
                            kind=CorporateActionKind.OPTION_ADJUST, from_symbol=ADJ_FROM,
                            to_symbol=ADJ_TO, ratio_to=D("3"), ratio_from=D("1"))
    conn.commit()
    text = _plain(build_ledgers_report_html(conn, now=NOW, frm=None, to=None).content)
    actions = text.split(" 公司行動 共 1 筆 ", 1)[1]
    assert f"選擇權合約調整 {ADJ_FROM} → {ADJ_TO} 1 → 3" in actions


# --- 現金收支明細 ---------------------------------------------------------------------------


def test_the_cash_statement_describes_option_lines_from_their_side(
    conn: sqlite3.Connection,
) -> None:
    art = build_cash_statement_csv(conn, account="schwab", ccy=None, now=NOW)
    assert art is not None
    rows = _csv_rows(art.content)
    cols = rows[0]
    by_ref = [dict(zip(cols, r, strict=True)) for r in rows[1:]]
    refs = [(r["symbol"], r["note_ref"]) for r in by_ref]
    assert refs == [
        (PUT, "賣出開倉 2 口 XYZ 2026-12-18 50 Put"),
        ("AAPL", "AAPL"),
        (CALL, "買進開倉 1 口 XYZ 2026-05-15 60 Call"),
        (EXP, "賣出開倉 1 口 ABC 2026-04-17 30 Put"),
        (CALL, "賣出平倉 1 口 XYZ 2026-05-15 60 Call"),
        (EXP, "到期作廢 1 口 ABC 2026-04-17 30 Put"),
    ]
    # The words are the ONE classifier's (§6.1) — what the ledgers wire and the cash page read.
    sides = option_sides_of(list_transactions(conn), list_corporate_actions(conn),
                            {i.symbol: i for i in list_instruments(conn)})
    assert sorted(sides.values()) == sorted(
        r["note_ref"].split(" ", 1)[0] for r in by_ref if r["symbol"] != "AAPL")
    html = build_cash_statement_report_html(conn, account="schwab", ccy=None, now=NOW)
    assert html is not None
    text = _plain(html.content)
    assert "賣出開倉 2 口 XYZ 2026-12-18 50 Put @ 2.00（費 1.30・稅 0.00）" in text
    assert "到期作廢 1 口 ABC 2026-04-17 30 Put @ 0.00（費 0.00・稅 0.00）" in text
    assert "買入 Apple（AAPL） 10 股 @ 100.00" in text


def test_a_refused_close_line_reads_unbooked(conn: sqlite3.Connection) -> None:
    _add_refused_close(conn)
    art = build_cash_statement_csv(conn, account="schwab", ccy=None, now=NOW)
    assert art is not None
    rows = _csv_rows(art.content)
    refs = [r[9] for r in rows[1:] if r[3] == REFUSED]
    assert refs == ["賣出開倉 1 口 JKL 2026-05-15 10 Put",
                    f"{OptionSide.UNBOOKED.value} 2 口 JKL 2026-05-15 10 Put"]


# --- 個股明細 / 再平衡報告 ------------------------------------------------------------------


def test_the_symbol_detail_csv_works_for_a_contract(conn: sqlite3.Connection) -> None:
    art = build_symbol_detail_csv(conn, symbol=PUT, today=NOW.date())
    assert art is not None
    assert art.filename == f"{PUT}_dividends.csv"
    rows = _csv_rows(art.content)
    assert rows[0][0] == "date" and rows[1:] == []      # a contract takes no dividend


def test_the_rebalance_report_excludes_options(conn: sqlite3.Connection) -> None:
    art = build_rebalance_report_html(conn, now=NOW, reporting=TWD,
                                      targets={"AAPL": D("0.5"), PUT: D("0.1")})
    text = _plain(art.content)
    assert "投資組合總市值（不含選擇權） 38,400 TWD" in text
    plan = text.split("摘要表（依標的）", 1)[1].split("彙總", 1)[0]
    assert PUT not in plan and "XYZ 2026-12-18" not in plan
