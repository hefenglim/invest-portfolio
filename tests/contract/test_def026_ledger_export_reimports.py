"""DEF-026 (functional test manual I-13, 2026-09-23): a ledger CSV the app EXPORTS must
import back as it is.

Measured: 交易帳本 › ⬇匯出 CSV (56 rows) pasted straight into CSV 匯入 › 交易 gave 56 ×
「✕ 錯誤 缺少必要欄位（欄位 account）」. The export (``export/ledgers.py::build_ledger_csv``)
dumps the TABLE — ``SELECT *``, so a reconciliation reproduces history byte for byte — and
four table columns are spelled differently from the import template (``account_id`` /
``quantity`` / ``fees`` / ``trade_date``). With the four headers renamed by hand the rows
wrote, but every ``fee_rule_snapshot`` was rewritten to ``{"engine": "supplied", …}``: the
provenance the export carried was replaced by a statement that the numbers were typed in.

Why no guard caught it: ``tests/contract/test_import_template.py`` proves the TEMPLATE
re-parses (template ↔ parser). Nothing proved the EXPORT re-parses (export ↔ parser), so the
two vocabularies drifted apart from the day the per-tab export shipped — for all six ledgers,
not only the one the report measured (``test_every_export_column_has_an_import_decision``
below is the missing guard).

Ruling: the importer accepts the export's column names as ALIASES (one table, in
``csv_import.py``); a supplied ``fee_rule_snapshot`` whose fee AND tax are supplied is kept
VERBATIM; an export-only column (id, batch, hash, …) is ignored but NAMED on the row, never
dropped in silence. The template's own column names do not change.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.csv_import import (
    EXPORT_COLUMN_ALIASES,
    EXPORT_ONLY_COLUMNS,
    TRANSACTION_IMPORT_ONLY_COLUMNS,
)
from portfolio_dash.data_ingestion.import_templates import template_columns
from portfolio_dash.data_ingestion.store import (
    insert_cash_movement,
    insert_corporate_action,
    insert_dividend,
    insert_fx_conversion,
    insert_transaction,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.ledger_registry import EXPORT_KINDS
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory

D = Decimal

#: Export tab key (``shared/ledger_registry.py``) -> import kind (``_BUILDERS``). Two of the
#: six are spelled differently on the two sides; a seventh ledger fails the guard below with
#: a KeyError until it is mapped — which is the point.
IMPORT_KIND = {
    "transactions": "transactions", "dividends": "dividends", "fx": "fx",
    "opening": "openings", "cash": "cash", "actions": "corporate_actions",
}

#: The columns a re-import legitimately gives NEW values: the row's own id, its import batch
#: and its row hash. Everything else must come back byte-identical.
_REGENERATED = {"id", "import_batch_id", "source_row_hash"}

#: Import order matters exactly as it does for a human doing this by hand: the cash pool
#: must exist before an FX conversion draws on it, the position before its dividend and its
#: corporate action.
_ORDER = ["opening", "cash", "transactions", "dividends", "fx", "actions"]

#: The owner's real snapshot shape (TW engine), a legacy empty one, and a v2 one.
_TW_SNAPSHOT = {"brokerage": "0.001425", "discount": "1", "min_fee": "20"}


def _instruments(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Semiconductors",
                                       name="TSMC", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="Apple"))


def _seed_source(conn: sqlite3.Connection) -> None:
    """One row or more in EVERY exportable ledger, through the real write paths."""
    _instruments(conn)
    upsert_opening(conn, account_id="tw_broker", symbol="2330", shares=D("1000"),
                   original_cost_total=D("500000"), build_date=date(2026, 1, 2))
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 1, 3),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=D("100000"), note="入金")
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=D("1000"), price=D("612.5"), fees=D("872"), tax=D("0"),
                       trade_date=date(2026, 1, 5), fee_rule_snapshot=_TW_SNAPSHOT,
                       note="測試, 含逗號")
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=D("10"), price=D("100.25"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 10))                      # legacy "{}"
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.SELL,
                       quantity=D("500"), price=D("620"), fees=D("441"), tax=D("930"),
                       trade_date=date(2026, 2, 5),
                       fee_rule_snapshot={"engine": "v2", "rounding": "floor"})
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=date(2026, 3, 1),
                    div_type="CASH", gross=D("5000"), withholding=D("0"), net=D("5000"))
    insert_dividend(conn, account_id="schwab", symbol="AAPL", div_date=date(2026, 3, 5),
                    div_type="DRIP", gross=D("2.5"), withholding=D("0.75"), net=D("1.75"),
                    reinvest_shares=D("0.0125"), reinvest_price=D("140"))
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 1, 8),
                         from_ccy=Currency.TWD, from_amount=D("32000"),
                         to_ccy=Currency.USD, to_amount=D("1000"))
    insert_corporate_action(conn, account_id="schwab", action_date=date(2026, 4, 1),
                            kind=CorporateActionKind.SPLIT, from_symbol="AAPL",
                            to_symbol="AAPL", ratio_to=D("2"), ratio_from=D("1"))
    conn.commit()


class _Pair:
    """A source DB that exports and a fresh destination DB that imports, both via the API."""

    def __init__(self, factory: DashboardClientFactory) -> None:
        self.src_conn: sqlite3.Connection | None = None
        self.dst_conn: sqlite3.Connection | None = None

        def src_seed(c: sqlite3.Connection) -> None:
            _seed_source(c)
            self.src_conn = c

        def dst_seed(c: sqlite3.Connection) -> None:
            _instruments(c)
            self.dst_conn = c

        self.src = factory(src_seed)
        self.dst = factory(dst_seed)

    def export(self, kind: str) -> str:
        r = self.src.post("/api/export/ledger", json={"kind": kind})
        assert r.status_code == 200, r.text
        return str(r.content.decode("utf-8-sig"))     # the downloaded file carries a BOM

    def rows(self, which: sqlite3.Connection | None, table: str) -> list[dict[str, object]]:
        assert which is not None
        return [{k: r[k] for k in r.keys() if k not in _REGENERATED}
                for r in which.execute(f"SELECT * FROM {table} ORDER BY rowid")]


@pytest.fixture
def pair(dashboard_client_factory: DashboardClientFactory) -> Iterator[_Pair]:
    yield _Pair(dashboard_client_factory)


def _round_trip(pair: _Pair, kinds: list[str]) -> dict[str, dict[str, object]]:
    previews: dict[str, dict[str, object]] = {}
    for kind in kinds:
        text = pair.export(kind)
        body = {"kind": IMPORT_KIND[kind], "csv_text": text}
        p = pair.dst.post("/api/import/preview", json=body)
        assert p.status_code == 200, p.text
        previews[kind] = p.json()
        c = pair.dst.post("/api/import/commit", json={**body, "ack_warnings": True})
        assert c.status_code == 200, c.text
    return previews


@pytest.mark.parametrize("kind", _ORDER)
def test_each_exported_ledger_previews_with_zero_errors(pair: _Pair, kind: str) -> None:
    """The reported symptom, per ledger: every row of the export parses and validates."""
    previews = _round_trip(pair, _ORDER[: _ORDER.index(kind) + 1])
    summary = previews[kind]["summary"]
    assert isinstance(summary, dict)
    assert summary["error"] == 0 and summary["total"] > 0, previews[kind]


@pytest.mark.parametrize("kind", _ORDER)
def test_each_ledger_writes_back_column_for_column(pair: _Pair, kind: str) -> None:
    """Export -> import -> every column equal, id / batch / hash aside. For transactions that
    includes ``fee_rule_snapshot``, TEXT for TEXT."""
    _round_trip(pair, _ORDER[: _ORDER.index(kind) + 1])
    table = EXPORT_KINDS[kind].table
    assert pair.rows(pair.dst_conn, table) == pair.rows(pair.src_conn, table)


def test_the_fee_rule_snapshot_survives_the_round_trip_verbatim(pair: _Pair) -> None:
    """The second half of the report: the rows wrote, and their provenance was replaced."""
    _round_trip(pair, ["opening", "cash", "transactions"])
    assert pair.dst_conn is not None
    stored = [r[0] for r in pair.dst_conn.execute(
        "SELECT fee_rule_snapshot FROM transactions ORDER BY id")]
    assert stored == [json.dumps(_TW_SNAPSHOT), "{}",
                      json.dumps({"engine": "v2", "rounding": "floor"})]
    assert not any('"supplied"' in s for s in stored)


def test_the_export_only_columns_are_named_on_every_row_not_dropped_silently(
    pair: _Pair,
) -> None:
    previews = _round_trip(pair, ["opening", "cash", "transactions"])
    rows = previews["transactions"]["rows"]
    assert isinstance(rows, list) and rows
    for row in rows:
        assert row["status"] == "ok", row
        info = " ".join(row.get("info") or [])
        assert "已忽略欄位：id、import_batch_id、source_row_hash" in info, row
        assert "fee_rule_snapshot" not in info   # read, not ignored


# ----------------------------------------------------- the parser's own rules (no HTTP)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    _instruments(c)
    c.commit()
    return c


def _txn_preview(csv_text: str) -> list[dict[str, object]]:
    from portfolio_dash.data_ingestion.csv_import import (
        build_transaction_preview,
        normalize_import_csv,
    )
    c = _conn()
    norm = normalize_import_csv(csv_text, "date")
    prev = build_transaction_preview(c, norm.text)
    return [{"issues": [(i.kind, i.message, i.info) for i in r.issues],
             "payload": r.payload} for r in prev.rows]


_EXPORT_HEADER = ("id,account_id,symbol,side,quantity,price,fees,tax,trade_date,"
                  "fee_rule_snapshot,note,daytrade,short_sale,import_batch_id,source_row_hash")


def test_a_blank_fee_discards_the_carried_snapshot_and_says_so() -> None:
    """A carried snapshot describes the numbers it was booked with. With the fee blank the
    ENGINE computes a new one, so the old record would describe a number it did not produce —
    it is dropped, and the row says so."""
    snap = json.dumps(_TW_SNAPSHOT).replace('"', '""')
    (row,) = _txn_preview(
        f"{_EXPORT_HEADER}\n7,tw_broker,2330,BUY,1000,612.5,,,2026-01-05,"
        f'"{snap}",,0,0,3,abc\n')
    payload = row["payload"]
    assert isinstance(payload, dict)
    written = {k[5:]: v for k, v in payload.items() if k.startswith("snap.")}
    assert written.get("engine") == "v2"                 # the engine's own, freshly made
    assert written != _TW_SNAPSHOT                       # ...not the carried record
    msgs = [m for _k, m, info in row["issues"] if info]  # type: ignore[attr-defined]
    assert any("fee_rule_snapshot" in m and "費用規則" in m for m in msgs), row


def test_an_unreadable_snapshot_is_a_loud_parse_error_in_chinese() -> None:
    (row,) = _txn_preview(
        f"{_EXPORT_HEADER}\n7,tw_broker,2330,BUY,1000,612.5,872,0,2026-01-05,"
        "not-json,,0,0,3,abc\n")
    hard = [(k, m) for k, m, info in row["issues"] if not info]  # type: ignore[attr-defined]
    assert hard and hard[0][0] == "parse_error"
    assert "fee_rule_snapshot" in hard[0][1] and "not-json" in hard[0][1]


def test_a_template_header_still_parses_exactly_as_before() -> None:
    """The alias table must not move the canonical door: template rows gain no advisory."""
    (row,) = _txn_preview(
        "account,symbol,side,date,shares,price,fee,tax,daytrade,short_sale,note\n"
        "tw_broker,2330,buy,2026-01-05,1000,612.5,872,0,,,\n")
    assert row["issues"] == []
    payload = row["payload"]
    assert isinstance(payload, dict)
    assert payload["snap.engine"] == "supplied"          # unchanged provenance rule


def test_a_header_carrying_both_spellings_keeps_the_template_one() -> None:
    """``account`` and ``account_id`` together: the template name wins, the other is named
    as ignored — never silently merged."""
    (row,) = _txn_preview(
        "account,account_id,symbol,side,date,shares,price\n"
        "tw_broker,schwab,2330,buy,2026-01-05,1000,612.5\n")
    payload = row["payload"]
    assert isinstance(payload, dict) and payload["account_id"] == "tw_broker"
    msgs = [m for _k, m, info in row["issues"] if info]  # type: ignore[attr-defined]
    assert any("account_id" in m for m in msgs), row


# ------------------------------------------------------------------- the missing guard


def test_every_export_column_has_an_import_decision() -> None:
    """export ↔ parser, the guard that did not exist: EVERY column the per-tab export writes
    either reaches a template column (directly or through ``EXPORT_COLUMN_ALIASES``), is a
    column the transaction door reads beyond its template (``fee_rule_snapshot``), or is
    declared in ``EXPORT_ONLY_COLUMNS`` with the reason it is dropped. A column added to a
    ledger table tomorrow fails here until someone decides which."""
    c = sqlite3.connect(":memory:")
    bootstrap_db(c)
    undecided: list[str] = []
    for export_kind, table in EXPORT_KINDS.items():
        template = set(template_columns(IMPORT_KIND[export_kind]))
        extra = TRANSACTION_IMPORT_ONLY_COLUMNS if export_kind == "transactions" else set()
        for (_cid, col, *_rest) in c.execute(f"PRAGMA table_info({table.table})"):
            name = EXPORT_COLUMN_ALIASES.get(col, col)
            if name in template or name in extra or col in EXPORT_ONLY_COLUMNS:
                continue
            undecided.append(f"{table.table}.{col}")
    assert undecided == []


def test_every_alias_lands_on_a_real_template_column() -> None:
    every_template = {col for k in IMPORT_KIND.values() for col in template_columns(k)}
    assert set(EXPORT_COLUMN_ALIASES.values()) <= every_template
    # An alias must never shadow a template name — that would re-route a canonical column.
    assert not set(EXPORT_COLUMN_ALIASES) & every_template



# ------------------------------------- the columns that deliberately do NOT come back


def _seed_exchange_source(conn: sqlite3.Connection) -> None:
    """An EXCHANGE that recorded a band move AND a weight move, plus its linked reorg fee —
    the three export columns whose value is a reference or a record, not ledger data."""
    from portfolio_dash.data_ingestion.store import MovedBand, MovedWeight

    _instruments(conn)
    upsert_instrument(conn, Instrument(symbol="NEWCO", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Semis",
                                       name="NewCo", board="TWSE"))
    insert_cash_movement(conn, account_id="tw_broker", move_date=date(2026, 1, 3),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=D("1000000"))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=D("1000"), price=D("500"), fees=D("712"), tax=D("0"),
                       trade_date=date(2026, 1, 5))
    action_id = insert_corporate_action(
        conn, account_id="tw_broker", action_date=date(2026, 6, 10),
        kind=CorporateActionKind.EXCHANGE, from_symbol="2330", to_symbol="NEWCO",
        ratio_to=D("1"), ratio_from=D("1"),
        band_move=MovedBand(from_symbol="2330", to_symbol="NEWCO", target_low=D("40")),
        weight_move=MovedWeight(from_symbol="2330", to_symbol="NEWCO", weight=D("0.25")))
    insert_cash_movement(conn, account_id="tw_broker", move_date=date(2026, 6, 10),
                         kind="WITHDRAW", ccy=Currency.TWD, amount=D("50"),
                         note="重組費", corporate_action_id=action_id)
    conn.commit()


def test_records_and_references_are_dropped_by_rule_and_the_rest_comes_back(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """``band_move_json`` / ``weight_move_json`` describe what the ORIGINAL save moved in the
    ORIGINAL database, and ``corporate_action_id`` points at an id the destination renumbers.
    Replaying any of them would assert something about the destination that never happened
    there, so they are declared in ``EXPORT_ONLY_COLUMNS`` and dropped; the import door
    re-derives its own records against the destination's own settings (none here -> NULL).
    Every other column still comes back byte-identical."""
    conns: dict[str, sqlite3.Connection] = {}

    def src_seed(c: sqlite3.Connection) -> None:
        _seed_exchange_source(c)
        conns["src"] = c

    def dst_seed(c: sqlite3.Connection) -> None:
        _instruments(c)
        upsert_instrument(c, Instrument(symbol="NEWCO", market=Market.TW,
                                        quote_ccy=Currency.TWD, sector="Semis",
                                        name="NewCo", board="TWSE"))
        conns["dst"] = c

    src, dst = dashboard_client_factory(src_seed), dashboard_client_factory(dst_seed)
    for kind in ("cash", "transactions", "actions"):
        text = src.post("/api/export/ledger", json={"kind": kind}).content.decode("utf-8-sig")
        body = {"kind": IMPORT_KIND[kind], "csv_text": text}
        p = dst.post("/api/import/preview", json=body).json()
        assert p["summary"]["error"] == 0, (kind, p)
        assert dst.post("/api/import/commit",
                        json={**body, "ack_warnings": True}).status_code == 200

    dropped = {"corporate_action_id", "band_move_json", "weight_move_json"}

    def rows(c: sqlite3.Connection, table: str) -> list[dict[str, object]]:
        return [{k: r[k] for k in r.keys() if k not in _REGENERATED | dropped}
                for r in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    for table in ("cash_movements", "corporate_actions"):
        assert rows(conns["dst"], table) == rows(conns["src"], table), table
    (a,) = conns["dst"].execute(
        "SELECT band_move_json, weight_move_json FROM corporate_actions").fetchall()
    assert tuple(a) == (None, None)
    assert [r[0] for r in conns["dst"].execute(
        "SELECT corporate_action_id FROM cash_movements ORDER BY rowid")] == [None, None]
