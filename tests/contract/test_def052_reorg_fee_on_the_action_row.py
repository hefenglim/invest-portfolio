"""DEF-052 (verifier R2 observation, spec 2026-09-24 §5): the corporate-action ledger row
shows the reorganisation fee linked to it — on the web row AND in the printed ledger report.

Measured on 4655845: 補登 2884 分割 1→2 ＋ 重組費用 50 → the ledger row read 「2026-09-24 台灣
券商 分割 2884 玉山金控 — 每 1 股 → 2 股 —」 with no fee; the API row carried ``reorg_fee`` but
only the delete confirm and the edit dialog read it. The printed 帳本報告 had no fee column at
all, and its footnote said 「公司行動不移動現金」 over an event that DID move 50 TWD.

The web row is pinned in a real browser (``tests/e2e/test_def040_def052_def060_flow.py``);
this file pins the printed report — the row BEHAVIOUR (the rendered HTML), not a string in the
builder's source.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_cash_movement
from portfolio_dash.shared.enums import Currency

_BASE = "/api/ledgers/corporate-actions"


def _fund(conn: sqlite3.Connection) -> None:
    insert_cash_movement(conn, account_id="tw_broker", move_date=date(2026, 1, 1),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("1000000"))
    conn.commit()


def _split(client: TestClient, *, fee: str | None) -> None:
    body = {"account_id": "tw_broker", "date": "2026-06-10", "kind": "SPLIT",
            "from_symbol": "2330", "to_symbol": "2330", "ratio_to": "2", "ratio_from": "1",
            "ack_warnings": True}
    if fee is not None:
        body["reorg_fee"] = fee
    r = client.post(_BASE, json=body)
    assert r.status_code == 201, r.text


def _action_section(doc: str) -> str:
    start = doc.index(">公司行動<")
    end = doc.index(">資金收支<", start)
    return doc[start:end]


def _cells(row: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", c).strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]


def test_the_printed_report_shows_the_fee_on_the_actions_own_row(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _fund(golden_db)
    _split(api_client, fee="50")
    doc = api_client.post("/api/export/ledgers-report", json={}).content.decode("utf-8")
    section = _action_section(doc)
    head = re.findall(r"<th[^>]*>(.*?)</th>", section)
    assert "重組費用" in head, head
    rows = re.findall(r"<tr>(.*?)</tr>", section, re.S)
    split = [r for r in rows if "分割" in r]
    assert len(split) == 1, rows
    cells = _cells(split[0])
    assert cells[head.index("重組費用")] == "50 TWD", cells
    # The fee is still a cash movement: the section names the link and does not total it.
    assert "資金收支" in section and "不另計合計" in section


def test_an_action_without_a_fee_prints_the_null_glyph_in_that_column(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _split(api_client, fee=None)
    doc = api_client.post("/api/export/ledgers-report", json={}).content.decode("utf-8")
    section = _action_section(doc)
    head = re.findall(r"<th[^>]*>(.*?)</th>", section)
    split = [r for r in re.findall(r"<tr>(.*?)</tr>", section, re.S) if "分割" in r]
    cells = _cells(split[0])
    assert cells[head.index("重組費用")] == "—", cells
