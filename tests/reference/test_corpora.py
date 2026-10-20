"""The corpora are what they claim to be (PERF plan W0.3).

Plus two RECORDS read from the committed v0.1.31 payloads (never from the live code, so they
stay true after a fix): what ``unquoted_ccy_cash`` shows of suspected defect PERF-F2, and that
``usd_reporting`` really carries 28-digit amounts into the trend's ``net_invested``.
"""

import json
import sqlite3
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.data_ingestion.store import (
    list_cash_movements,
    list_dividends,
    list_transactions,
    load_ledger_bundle,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.returns import XIRR_CASH_KINDS
from portfolio_dash.pricing.store import get_fx_history
from portfolio_dash.shared.enums import Currency
from tests.reference import corpora
from tests.reference.corpora import CORPORA, CORPUS_NAMES, copy_corpus, open_db

_PAYLOADS = Path(__file__).resolve().parent / "payloads"


@pytest.fixture
def connect(corpus_template_dir: Path, tmp_path: Path) -> Iterator[Any]:
    conns: list[sqlite3.Connection] = []

    def _open(name: str) -> sqlite3.Connection:
        db = copy_corpus(name, corpus_template_dir, tmp_path / f"{name}-{len(conns)}.db")
        conns.append(open_db(db.path))
        return conns[-1]

    yield _open
    for conn in conns:
        conn.close()


def _payload(name: str, rel: str = "dashboard.json") -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((_PAYLOADS / name / rel).read_bytes())
    return doc


@pytest.mark.parametrize("name", CORPUS_NAMES)
def test_every_corpus_has_a_fixed_taipei_clock(name: str) -> None:
    corpus = CORPORA[name]
    assert corpus.now.tzinfo == ZoneInfo("Asia/Taipei")
    assert corpus.name == name


def test_templates_are_file_backed_wal_databases(corpus_template_dir: Path) -> None:
    path = corpora.ensure_template("golden", corpus_template_dir)
    assert path.is_file() and path.stat().st_size > 0
    conn = open_db(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_doubled_demo_repeats_every_row_before_now_at_plus_one_day(connect: Any) -> None:
    demo, doubled = connect("demo"), connect("doubled_demo")
    now_day = corpora.DEMO_NOW.date()
    for lister, day in ((list_transactions, "trade_date"), (list_dividends, "date")):
        originals = lister(demo)
        rows = lister(doubled)
        assert all(getattr(r, day) < now_day for r in originals)  # nothing is left out
        assert len(rows) == 2 * len(originals)
        copies = sorted(rows, key=lambda r: r.id)[len(originals):]  # written after them all
        for orig, copy in zip(sorted(originals, key=lambda r: (getattr(r, day), r.id)), copies,
                              strict=True):
            assert getattr(copy, day) == getattr(orig, day) + timedelta(days=1)
            fields = orig.model_dump(exclude={"id", day})
            assert copy.model_dump(exclude={"id", day}) == fields
    for table in ("transactions", "dividends"):
        assert doubled.execute(
            f"SELECT COUNT(*) FROM {table} WHERE import_batch_id IS NOT NULL").fetchone()[0] == 0


def test_doubled_demo_is_a_valid_ledger(connect: Any) -> None:
    conn = connect("doubled_demo")
    bundle = load_ledger_bundle(conn).valued_as_of(corpora.DEMO_NOW.date())
    build_book(bundle)  # the strict path raises on any oversell or unbookable row
    for d in sorted(bundle.counts_on_dates()):
        book = build_book(bundle.through(d), allow_oversell=True)
        assert not (book.oversold_positions or book.refused_dividends or book.unapplied_actions
                    or book.refused_closes), d


def test_double_before_refuses_a_copy_landing_on_now(connect: Any) -> None:
    conn = connect("golden")
    with pytest.raises(ValueError, match="on or after"):
        corpora.double_before(conn, corpora.GOLDEN_NOW.date().replace(month=1, day=6))


def test_unquoted_ccy_cash_has_its_shape(connect: Any) -> None:
    conn = connect("unquoted_ccy_cash")
    quoted = {i.quote_ccy for i in load_ledger_bundle(conn).instruments.values()}
    moves = [m for m in list_cash_movements(conn) if m.kind.upper() in XIRR_CASH_KINDS]
    assert len(moves) == 1
    move = moves[0]
    assert move.ccy is Currency.MYR and move.ccy not in quoted
    rates = get_fx_history(conn, Currency.MYR, Currency.TWD, move.date - timedelta(days=365),
                           move.date)
    assert rates, "the rebate's rate must be stored on or before its date"


def test_record_perf_f2_in_v0131(connect: Any) -> None:
    """What v0.1.31 DID on ``unquoted_ccy_cash`` (spec §8 item 5, PERF-F2): the rebate's rate is
    stored and XIRR uses it, but the trend loads only the ledger's quote currencies, finds no
    MYR rate, and the whole trend — and with it net worth, B, B − A and the benchmark — is
    unavailable. A fix changes these payloads; it is its own change, not part of the store."""
    golden, f2 = _payload("golden"), _payload("unquoted_ccy_cash")
    assert golden["trend"]["available"] is True
    assert f2["trend"] == {"points": [], "reporting_currency": "TWD", "available": False}
    assert f2["freshness"]["trend_unavailable_reason"] == "帳本中有交易日期缺少匯率資料"
    assert f2["kpis"]["xirr"] is not None and f2["kpis"]["xirr"] != golden["kpis"]["xirr"]
    for key in ("total_return_fx_complete", "principal_fx_effect", "trading_financing_cost"):
        assert f2["kpis"][key] is None, key
    assert f2["kpis"]["fx_complete_reason"] == "尚無趨勢資料，無法計算含匯兌損益"
    assert f2["benchmark"]["available"] is False
    assert f2["benchmark"]["reason"] == (
        "部分投入流量缺少當日匯率，無法換算成報告幣，基準對照暫不可用")
    # The month ends before the rebate (dated 2026-03-16) still have a trend; from March on
    # the whole series is gone.
    for month, available in (("2026-01", True), ("2026-02", True), ("2026-03", False),
                             ("2026-05", False)):
        assert _payload("unquoted_ccy_cash", f"month_end/{month}.json")["trend"]["available"] \
            is available, month


def test_usd_reporting_carries_28_digit_amounts_into_the_trend() -> None:
    trend = _payload("usd_reporting")["trend"]
    assert trend["available"] is True
    digits = max(len(Decimal(p["net_invested"]).as_tuple().digits) for p in trend["points"])
    assert digits >= 28


def _shapes(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    """What a corpus's ledger holds, as the replay sees it at the corpus clock."""
    bundle = load_ledger_bundle(conn)
    today = CORPORA[name].now.date()
    book = build_book(bundle.without_unregistered().valued_as_of(today), allow_oversell=True)
    options = {s for s, i in bundle.instruments.items() if i.is_option}
    return {
        "stock_realized": sorted({r.kind for r in book.realized.rows if r.symbol not in options}),
        "option_trades": sorted({(t.side.value, t.short_sale, t.close_reason or "")
                                 for t in bundle.transactions if t.symbol in options}),
        "option_cover": any(r.kind == "short_cover" and r.symbol in options
                            for r in book.realized.rows),
        "actions": sorted({a.kind.value for a in bundle.actions}),
        "dividends": sorted({d.type.value for d in bundle.dividends}),
        "future_rows": sum(1 for t in bundle.transactions if t.trade_date > today)
        + sum(1 for d in bundle.dividends if d.effective_date > today),
        "unreadable": len(bundle.unreadable_actions),
        "oversold": len(book.oversold_positions),
        "orphan": len(book.orphan_dividends),
        "unbookable": [u.cause for u in book.unbookable_dividends],
        "refused_closes": len(book.refused_closes),
        "unapplied": len(book.unapplied_actions),
    }


def test_the_rich_corpora_hold_their_shapes(connect: Any) -> None:
    """The shapes the wave-1 review found missing from the oracle, pinned so an edited seed
    cannot quietly drop one (README "The rich corpora")."""
    assert _shapes(connect("rich"), "rich") == {
        "stock_realized": ["sale", "short_cover"],
        "option_trades": [("BUY", False, ""), ("SELL", False, "expired"), ("SELL", True, "")],
        "option_cover": True,
        "actions": ["EXCHANGE", "OPTION_ADJUST", "SPINOFF", "SPLIT"],
        "dividends": ["CASH", "DRIP", "NET", "STOCK"],
        "future_rows": 2, "unreadable": 0, "oversold": 0, "orphan": 0, "unbookable": [],
        "refused_closes": 0, "unapplied": 0,
    }
    refusals = _shapes(connect("rich_refusals"), "rich_refusals")
    assert (refusals["unreadable"], refusals["oversold"], refusals["orphan"],
            refusals["unbookable"], refusals["refused_closes"], refusals["unapplied"]) == (
        1, 1, 1, ["short"], 1, 2)
    conn = connect("rich_refusals")
    # a holding never priced, and the FX gap on both fetched pairs
    assert conn.execute("SELECT COUNT(*) FROM prices WHERE instrument = 'NOPX'"
                        ).fetchone()[0] == 0
    for base, quote in ((Currency.USD, Currency.TWD), (Currency.USD, Currency.MYR)):
        assert get_fx_history(conn, base, quote, date(2026, 2, 2), date(2026, 2, 20)) == []


def test_epoch1990_has_its_shape(connect: Any) -> None:
    conn = connect("epoch1990")
    first_close = conn.execute("SELECT MIN(as_of_date) FROM prices WHERE instrument = '^GSPC'"
                               ).fetchone()[0]
    assert first_close < "1990-01-01"
    [opening] = load_ledger_bundle(conn).opening
    assert opening.build_date.year == 1988
    rates = get_fx_history(conn, Currency.USD, Currency.TWD, date(1900, 1, 1),
                           date(1990, 12, 31))
    assert [r.as_of.year for r in rates] == [1987]


def test_record_the_1990_floor_in_v0131() -> None:
    """RECORD (the committed v0.1.31 payload): the 1988 flow is uncovered — v0.1.31 reads the
    index from 1990-01-01 although closes from 1987 are stored."""
    bench = _payload("epoch1990")["benchmark"]
    assert bench["available"] is True and bench["uncovered_markets"] == ["US"]
