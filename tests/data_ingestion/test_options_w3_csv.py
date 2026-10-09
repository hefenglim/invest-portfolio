"""Options W3 — the transaction CSV door and its template (spec §8, §10).

An OCC symbol resolves at the preview WITHOUT a write (「將建立選擇權合約 …」) and is
registered only inside the commit's transaction; ``close_reason`` is a column; a close row
costs nothing and never reaches the engine; OPT-D6's unset rate asks for the statement's
fee; a put premium above the strike is questioned; the raw ledger export with option rows
re-imports column for column.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.csv_import import (
    TRANSACTION_COLUMNS,
    build_transaction_preview,
    normalize_import_csv,
    write_transaction_row,
)
from portfolio_dash.data_ingestion.import_templates import (
    _TRANSACTION_ROWS,
    annotated_columns,
)
from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow, commit_preview
from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import (
    get_instrument,
    insert_transaction,
    list_transactions,
)
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory

PUT = "XYZ261218P00050000"
HEADER = ",".join(TRANSACTION_COLUMNS)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    seed_accounts(c)
    yield c
    c.close()


def _preview(conn: sqlite3.Connection, body: str) -> ImportPreview:
    norm = normalize_import_csv(f"{HEADER}\n{body}", "date")
    return build_transaction_preview(conn, norm.text)


def _kinds(row: PreviewRow) -> set[str]:
    return {i.kind for i in row.issues}


def _hard(row: PreviewRow) -> set[str]:
    return {i.kind for i in row.issues if not i.needs_confirm}


# --- template -----------------------------------------------------------------------------


def test_close_reason_joins_the_template_last_and_optional() -> None:
    assert TRANSACTION_COLUMNS[-1] == "close_reason"
    assert annotated_columns("transactions")[-1] == "close_reason（選填）"


def test_the_template_carries_an_option_example_with_its_unit_note() -> None:
    options = [r for r in _TRANSACTION_ROWS if r[1] == PUT]
    assert options, "no option example row"
    assert "2 口＝200" in options[0][10]
    assert options[0][4] == "200"


# --- preview never writes, commit registers -----------------------------------------------


def test_an_unregistered_contract_previews_without_a_write(conn: sqlite3.Connection) -> None:
    (row,) = _preview(conn, f"schwab,{PUT},sell,2026-11-02,200,1.50,,,,1,,").rows
    assert _hard(row) == set(), row.issues
    info = [i.message for i in row.issues if i.info]
    assert any(m.startswith("將建立選擇權合約 XYZ 2026-12-18 50 Put") for m in info), info
    assert get_instrument(conn, PUT) is None          # the preview wrote nothing
    # 2 contracts × 0.65 + SEC on the 300 premium sold (minimum 0.01).
    assert row.fee == Decimal("1.31") and row.tax == Decimal("0")


def test_the_commit_registers_the_contract_in_its_own_transaction(
    conn: sqlite3.Connection,
) -> None:
    preview = _preview(conn, f"schwab,{PUT},sell,2026-11-02,200,1.50,,,,1,,")
    summary = commit_preview(conn, preview, accept={0}, writer=write_transaction_row)
    assert len(summary.written) == 1
    inst = get_instrument(conn, PUT)
    assert inst is not None and inst.is_option
    (tx,) = list_transactions(conn, symbol=PUT)
    assert tx.short_sale and tx.quantity == Decimal("200")


def test_a_failed_batch_rolls_the_registration_back(conn: sqlite3.Connection) -> None:
    preview = _preview(conn, f"schwab,{PUT},sell,2026-11-02,200,1.50,,,,1,,")

    def boom(conn: sqlite3.Connection, row: PreviewRow, *, commit: bool = True) -> int:
        write_transaction_row(conn, row, commit=commit)
        raise RuntimeError("second step failed")

    with pytest.raises(RuntimeError):
        commit_preview(conn, preview, accept={0}, writer=boom)
    assert get_instrument(conn, PUT) is None
    assert list_transactions(conn, symbol=PUT) == []


# --- close rows ----------------------------------------------------------------------------


def test_a_close_row_costs_nothing_and_never_reaches_the_engine(
    conn: sqlite3.Connection,
) -> None:
    register_option_contract(conn, PUT)
    insert_transaction(conn, account_id="schwab", symbol=PUT, side=Side.SELL,
                       quantity=Decimal("200"), price=Decimal("1.5"), fees=Decimal("1.3"),
                       tax=Decimal("0"), trade_date=date(2026, 11, 2), short_sale=True)
    (row,) = _preview(conn, f"schwab,{PUT},buy,2026-12-18,200,0,,,,,,expired").rows
    assert _hard(row) == set(), row.issues
    assert (row.fee, row.tax) == (Decimal("0"), Decimal("0"))
    assert {k: v for k, v in row.payload.items() if k.startswith("snap.")} == {
        "snap.source": "close_reason"}
    commit_preview(conn, ImportPreview(rows=[row]), accept={0}, writer=write_transaction_row)
    closes = [t for t in list_transactions(conn, symbol=PUT) if t.close_reason]
    assert [(t.close_reason, t.price, t.fees) for t in closes] == [
        ("expired", Decimal("0"), Decimal("0"))]


def test_a_close_row_with_a_fee_is_refused(conn: sqlite3.Connection) -> None:
    register_option_contract(conn, PUT)
    insert_transaction(conn, account_id="schwab", symbol=PUT, side=Side.SELL,
                       quantity=Decimal("200"), price=Decimal("1.5"), fees=Decimal("1.3"),
                       tax=Decimal("0"), trade_date=date(2026, 11, 2), short_sale=True)
    (row,) = _preview(conn, f"schwab,{PUT},buy,2026-12-18,200,0,0.65,,,,,expired").rows
    assert "close_row_fee" in _hard(row)


def test_the_close_reason_cell_accepts_the_zh_words_and_refuses_others(
    conn: sqlite3.Connection,
) -> None:
    rows = _preview(conn, (
        f"schwab,{PUT},buy,2026-12-18,200,0,,,,,,到期作廢\n"
        f"schwab,{PUT},buy,2026-12-18,200,0,,,,,,closed\n")).rows
    assert "parse_error" not in _kinds(rows[0])
    bad = [i for i in rows[1].issues if i.kind == "parse_error"]
    assert bad and "close_reason" in bad[0].message


# --- fees (OPT-D6) ---------------------------------------------------------------------------


def test_an_unset_rate_needs_the_statement_fee(conn: sqlite3.Connection) -> None:
    rows = _preview(conn, (
        f"moomoo_my,{PUT},sell,2026-11-02,100,1.50,,,,1,,\n"
        f"moomoo_my,{PUT},sell,2026-11-03,100,1.50,1.99,,,1,,\n")).rows
    unset = [i for i in rows[0].issues if i.kind == "option_fee_unset"]
    assert unset and not unset[0].needs_confirm
    assert unset[0].message.startswith("此帳戶未設定選擇權手續費")
    assert "option_fee_unset" not in _kinds(rows[1])
    assert (rows[1].fee, rows[1].tax) == (Decimal("1.99"), Decimal("0"))
    assert "stamp_fx_missing" not in _kinds(rows[1])   # no MY stamp on an option


def test_a_put_premium_above_the_strike_is_questioned(conn: sqlite3.Connection) -> None:
    (row,) = _preview(conn, f"schwab,{PUT},sell,2026-11-02,100,60,,,,1,,").rows
    hit = [i for i in row.issues if i.kind == "option_premium_exceeds_strike"]
    assert hit and hit[0].needs_confirm and not hit[0].info


def test_a_stock_row_payload_carries_no_close_reason_key(conn: sqlite3.Connection) -> None:
    from portfolio_dash.data_ingestion.store import upsert_instrument
    from portfolio_dash.shared.enums import Currency, Market
    from portfolio_dash.shared.models.assets import Instrument

    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="Apple"))
    (row,) = _preview(conn, "schwab,AAPL,buy,2026-01-05,10,100,,,,,,").rows
    assert "close_reason" not in row.payload


# --- the corporate-action and opening CSV doors ------------------------------------------------


def test_the_action_csv_accepts_option_adjust_and_registers_on_commit(
    conn: sqlite3.Connection,
) -> None:
    from portfolio_dash.data_ingestion.corporate_action_import import (
        CORPORATE_ACTION_COLUMNS,
        build_corporate_action_preview,
        corporate_action_writer,
    )
    from portfolio_dash.data_ingestion.store import list_corporate_actions

    register_option_contract(conn, PUT)
    insert_transaction(conn, account_id="schwab", symbol=PUT, side=Side.SELL,
                       quantity=Decimal("100"), price=Decimal("1.5"), fees=Decimal("0.66"),
                       tax=Decimal("0"), trade_date=date(2026, 5, 4), short_sale=True)
    to = "XYZ261218P00025000"
    text = (",".join(CORPORATE_ACTION_COLUMNS) + "\n"
            f"schwab,2026-06-01,選擇權合約調整,{PUT},{to},2,1,,1 → 2\n")
    preview = build_corporate_action_preview(conn, text)
    (row,) = preview.rows
    assert not row.has_hard_issue, row.issues
    assert row.payload["kind"] == "OPTION_ADJUST"
    assert row.payload["kind_label"] == "選擇權合約調整"
    assert get_instrument(conn, to) is None
    writer = corporate_action_writer(move_weight=lambda _f, _t: None)
    commit_preview(conn, preview, accept={0}, writer=writer)
    inst = get_instrument(conn, to)
    assert inst is not None and inst.is_option
    assert [a.kind for a in list_corporate_actions(conn)] == ["OPTION_ADJUST"]


def test_the_opening_csv_refuses_an_option(conn: sqlite3.Connection) -> None:
    from portfolio_dash.data_ingestion.opening_import import (
        OPENING_COLUMNS,
        build_opening_preview,
    )

    text = ",".join(OPENING_COLUMNS) + f"\nschwab,{PUT},100,150,2026-01-02,\n"
    (row,) = build_opening_preview(conn, text).rows
    hit = [i for i in row.issues if i.kind == "opening_on_option"]
    assert hit and not hit[0].needs_confirm and hit[0].message == "選擇權請以交易登錄"


# --- the raw export with option rows re-imports column for column -------------------------


def _seed_options(c: sqlite3.Connection) -> None:
    seed_accounts(c)
    register_option_contract(c, PUT)
    insert_transaction(c, account_id="schwab", symbol=PUT, side=Side.SELL,
                       quantity=Decimal("200"), price=Decimal("1.5"), fees=Decimal("1.3"),
                       tax=Decimal("0"), trade_date=date(2026, 11, 2), short_sale=True,
                       fee_rule_snapshot={"engine": "v2", "option": "true"})
    insert_transaction(c, account_id="schwab", symbol=PUT, side=Side.BUY,
                       quantity=Decimal("200"), price=Decimal("0"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 12, 18),
                       fee_rule_snapshot={"source": "close_reason"}, close_reason="expired")
    c.commit()


def test_the_raw_export_with_option_rows_round_trips(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    holder: dict[str, sqlite3.Connection] = {}

    def src(c: sqlite3.Connection) -> None:
        _seed_options(c)
        holder["src"] = c

    def dst(c: sqlite3.Connection) -> None:
        seed_accounts(c)
        holder["dst"] = c

    source, dest = dashboard_client_factory(src), dashboard_client_factory(dst)
    r = source.post("/api/export/ledger", json={"kind": "transactions"})
    assert r.status_code == 200, r.text
    text = r.content.decode("utf-8-sig")
    assert "close_reason" in text.splitlines()[0]
    body = {"kind": "transactions", "csv_text": text}
    p = dest.post("/api/import/preview", json=body)
    assert p.status_code == 200, p.text
    assert p.json()["summary"]["error"] == 0, p.json()
    c = dest.post("/api/import/commit", json={**body, "ack_warnings": True})
    assert c.status_code == 200, c.text
    skip = {"id", "import_batch_id", "source_row_hash"}

    def rows(conn: sqlite3.Connection) -> list[dict[str, object]]:
        return [{k: r[k] for k in r.keys() if k not in skip}
                for r in conn.execute("SELECT * FROM transactions ORDER BY rowid")]

    assert rows(holder["dst"]) == rows(holder["src"])
    inst = get_instrument(holder["dst"], PUT)
    assert inst is not None and inst.is_option
