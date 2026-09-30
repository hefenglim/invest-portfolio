"""Post-closure item 10 (owner 2026-09-30): the ledger audit trail finally has a reader.

Every edit / delete of a ledger row writes the row's BEFORE image to ``ledger_audit`` (audit
M9) — the demo had 353 rows — and nothing read it back: ``store.list_ledger_audit`` had no
caller, no route served it, no page listed it. The owner's ruling: a READ-ONLY list in 資料中心
(time, ledger, row, action, the pre-change content rendered readably, newest first, paged) and
a CSV in the export centre beside the other logs.

What this file pins, beyond "the route answers":

* **zh words, derived coverage.** The tables ``_write_audit`` is called with are read out of
  ``store.py`` by AST, and every column of every one of them out of the schema — each must have
  a zh label, so a new audited table or column cannot reach the page as a raw identifier.
* **Accounts by token (DEF-044 / DEF-045).** The before-image carries ``account_id`` and the
  期初庫存 row key is ``<account>/<symbol>``; neither may reach a rendered string (or a CSV cell
  outside the ``account_id`` column) as a raw id — only as ``{account:<id>}``, which the fetch
  layer resolves.
* **The page's clock.** ``ledger_audit.at`` is stored in UTC; the wire says Asia/Taipei, because
  ``web/format.js::datetime`` slices a string it trusts to be local already.
"""

from __future__ import annotations

import ast
import csv
import io
import re
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.provenance import undo_source
from portfolio_dash.data_ingestion.store import (
    audit_source,
    delete_cash_movement,
    delete_dividend,
    insert_cash_movement,
    list_dividends,
    list_transactions,
    update_transaction,
    upsert_opening,
)
from portfolio_dash.shared.account_ref import ACCOUNT_REF_RE
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import Side
from tests.contract.test_def045_exports_never_print_account_labels import (
    _account_ids,
    _csv_offenders,
)

_STORE = Path(__file__).resolve().parents[2] / "portfolio_dash" / "data_ingestion" / "store.py"
_CSV_HEADER = ["audit_id", "at", "table", "table_label", "row", "action", "action_label",
               "source", "account_id", "before"]


def _bare_ids(text: str, ids: list[str]) -> list[str]:
    text = ACCOUNT_REF_RE.sub(" ", text)
    return [i for i in ids if re.search(rf"(?<![\w]){re.escape(i)}(?![\w])", text)]


@pytest.fixture
def audited(golden_db: sqlite3.Connection) -> sqlite3.Connection:
    """Four corrections through the REAL store doors, one per shape the reader must render:
    an edit, a delete, the keyed 期初庫存 edit, and a batch-undo delete (DEF-049 source)."""
    conn = golden_db
    txn = next(t for t in list_transactions(conn) if t.symbol == "2330")
    update_transaction(conn, txn.id, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1200"), price=Decimal("500"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 5), daytrade=False)
    div = next(d for d in list_dividends(conn) if d.symbol == "2330")
    delete_dividend(conn, div.id)
    upsert_opening(conn, account_id="schwab", symbol="AAPL", shares=Decimal("3"),
                   original_cost_total=Decimal("270"), build_date=date(2025, 12, 1))
    upsert_opening(conn, account_id="schwab", symbol="AAPL", shares=Decimal("4"),
                   original_cost_total=Decimal("360"), build_date=date(2025, 12, 1))
    move = insert_cash_movement(conn, account_id="moomoo_my", move_date=date(2026, 2, 1),
                                kind="DEPOSIT", ccy=Currency.MYR, amount=Decimal("9000.50"))
    with audit_source(undo_source(7)):
        delete_cash_movement(conn, move)
    conn.commit()
    return conn


def _rows(client: TestClient, **params: Any) -> dict[str, Any]:
    r = client.get("/api/ledger-audit", params=params)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def _fields(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["field"]: f for f in row["fields"]}


# ------------------------------------------------------------------------- the list


def test_the_list_is_newest_first_with_zh_labels(
    api_client: TestClient, audited: sqlite3.Connection
) -> None:
    body = _rows(api_client)
    assert body["total_count"] == 4
    rows = body["rows"]
    assert [r["id"] for r in rows] == sorted((r["id"] for r in rows), reverse=True)
    assert [(r["table_label"], r["action_label"]) for r in rows] == [
        ("資金收支", "刪除"), ("期初庫存", "編輯"), ("股利帳本", "刪除"), ("交易帳本", "編輯")]
    assert [r["table"] for r in rows] == [
        "cash_movements", "opening_inventory", "dividends", "transactions"]
    assert [r["source"] for r in rows] == ["批次復原 #7", None, None, None]


def test_the_before_image_is_rendered_as_labelled_fields(
    api_client: TestClient, audited: sqlite3.Connection
) -> None:
    rows = {r["table"]: r for r in _rows(api_client)["rows"]}
    txn = _fields(rows["transactions"])
    assert txn["quantity"] == {"field": "quantity", "label": "股數", "value": "1000"}  # BEFORE
    assert txn["side"]["value"] == "買"
    assert txn["daytrade"]["value"] == "否"
    assert txn["account_id"] == {"field": "account_id", "label": "帳戶",
                                 "value": "{account:tw_broker}"}
    cash = _fields(rows["cash_movements"])
    assert cash["kind"]["value"] == "入金"
    assert cash["amount"]["value"] == "9000.50"      # as stored, full precision
    assert cash["note"]["value"] is None             # the page prints its null glyph
    assert rows["transactions"]["summary"] == "{account:tw_broker}・2330・2026-01-05・買"


def test_the_opening_row_key_names_its_account_by_token(
    api_client: TestClient, audited: sqlite3.Connection
) -> None:
    """``opening_inventory`` is keyed ``<account>/<symbol>`` — the one row id that embeds an
    account. The wire keeps the stored key in ``row_id`` and gives the page ``row_label``."""
    row = next(r for r in _rows(api_client)["rows"] if r["table"] == "opening_inventory")
    assert row["row_id"] == "schwab/AAPL"
    assert row["row_label"] == "{account:schwab}／AAPL"
    assert _fields(row)["shares"]["value"] == "3"


def test_no_rendered_string_prints_a_raw_account_id(
    api_client: TestClient, audited: sqlite3.Connection
) -> None:
    ids = _account_ids(audited)
    problems: list[str] = []
    for row in _rows(api_client)["rows"]:
        shown = [row["row_label"], row["summary"], row["table_label"], row["action_label"],
                 row["source"] or "", *(f["label"] for f in row["fields"]),
                 *(f["value"] or "" for f in row["fields"])]
        for text in shown:
            problems += [f"{row['table']} #{row['id']}: {text!r}"
                         for _ in _bare_ids(text, ids)]
    assert not problems, problems


def test_the_time_is_the_app_clock_not_utc(
    api_client: TestClient, audited: sqlite3.Connection
) -> None:
    stored = audited.execute("SELECT at FROM ledger_audit ORDER BY id DESC").fetchone()["at"]
    assert stored.endswith("+00:00")
    shown = _rows(api_client)["rows"][0]["at"]
    assert shown.endswith("+08:00"), shown


def test_the_list_pages(api_client: TestClient, audited: sqlite3.Connection) -> None:
    first = _rows(api_client, limit=2, offset=0)
    second = _rows(api_client, limit=2, offset=2)
    assert first["total_count"] == second["total_count"] == 4
    ids = [r["id"] for r in first["rows"]] + [r["id"] for r in second["rows"]]
    assert len(ids) == 4 and ids == sorted(ids, reverse=True)
    for bad in (0, 501):
        assert api_client.get("/api/ledger-audit", params={"limit": bad}).status_code == 400


def test_an_empty_trail_is_an_empty_list(api_client: TestClient) -> None:
    assert _rows(api_client) == {"rows": [], "total_count": 0}


# ------------------------------------------------------------- derived label coverage


def _audited_tables() -> set[str]:
    """Every table name ``store.py`` passes to ``_write_audit`` — read, not listed."""
    tree = ast.parse(_STORE.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "_write_audit":
            arg = node.args[1]
            assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), \
                ast.unparse(node)
            out.add(arg.value)
    return out


def test_every_audited_table_and_column_has_a_zh_label(golden_db: sqlite3.Connection) -> None:
    from portfolio_dash.export.ledger_audit import ACTION_LABELS, FIELD_LABELS, TABLE_LABELS

    tables = _audited_tables()
    assert {"transactions", "dividends", "opening_inventory", "instruments"} <= tables
    assert not tables - set(TABLE_LABELS), tables - set(TABLE_LABELS)
    ddl = golden_db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'ledger_audit'").fetchone()["sql"]
    actions = set(re.findall(r"'(\w+)'", ddl.split("CHECK", 1)[1]))
    assert actions == set(ACTION_LABELS) == {"update", "delete"}
    missing = [f"{t}.{c['name']}" for t in sorted(tables)
               for c in golden_db.execute(f"PRAGMA table_info({t})")
               if c["name"] not in FIELD_LABELS]
    assert not missing, missing


# ------------------------------------------------------------------------ the CSV


def _csv(client: TestClient, body: dict[str, Any]) -> tuple[bytes, list[list[str]]]:
    r = client.post("/api/export/ledger-audit", json=body)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "ledger_audit" in r.headers["content-disposition"]
    return r.content, list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))


def test_the_csv_carries_every_row_chronologically(
    api_client: TestClient, audited: sqlite3.Connection
) -> None:
    raw, rows = _csv(api_client, {})
    assert raw.startswith(b"\xef\xbb\xbf")
    assert rows[0] == _CSV_HEADER
    body = rows[1:]
    assert len(body) == 4
    assert [int(r[0]) for r in body] == sorted(int(r[0]) for r in body)   # oldest first
    by_table = {r[2]: dict(zip(_CSV_HEADER, r, strict=True)) for r in body}
    opening = by_table["opening_inventory"]
    assert opening["row"] == "{account:schwab}／AAPL"
    assert opening["account_id"] == "schwab"
    assert opening["action_label"] == "編輯" and opening["table_label"] == "期初庫存"
    assert "帳戶：{account:schwab}" in opening["before"]
    assert "股數：3" in opening["before"]
    assert opening["at"].endswith("+08:00")
    assert by_table["cash_movements"]["source"] == "批次復原 #7"
    assert by_table["transactions"]["source"] == ""        # a single-row correction


def test_the_csv_never_prints_a_raw_account_id_outside_the_id_column(
    api_client: TestClient, audited: sqlite3.Connection
) -> None:
    raw, _ = _csv(api_client, {})
    assert _csv_offenders(raw.decode("utf-8-sig"), _account_ids(audited)) == []


def test_the_csv_honours_the_date_range(
    api_client: TestClient, audited: sqlite3.Connection
) -> None:
    _, rows = _csv(api_client, {"from": "2099-01-01"})
    assert rows == [_CSV_HEADER]
    r = api_client.post("/api/export/ledger-audit", json={"from": "2026-02-01", "to": "2026-01-01"})
    assert r.status_code == 400
