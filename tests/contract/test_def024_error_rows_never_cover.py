"""DEF-024 (functional test I-06, 2026-09-22): an ERROR row lends no shares, and a skipped
row says why.

**Measured defect.** Holding 2884 × 100. A pasted CSV of 「buy 1.5」 + 「sell 101」 previewed
row 1 as ✕ (台股股數必須是整數) and row 2 as ✓ 可寫入 — the 1.5 shares the buy could never
book were counted as cover — and the commit answered ``written 0 / skipped 1 / rejected 1``
with the sell dropped into 「跳過」 and no reason anywhere. A second file, 「buy 100」 +
「sell 150」 with the buy unticked, toasted 「✓ 寫入成功 成功 0 筆・跳過 2 筆」.

Root cause 1 — ``csv_import.build_transaction_preview`` decided sibling membership on the
STRUCTURAL prefix alone (``transaction_structural_issues``), and ``shares_not_integer`` is
not structural: it needs the instrument's market. ``validate.pending_share_flows`` now
applies every ROW-LEVEL hard finding (``row_cannot_be_written``), so the 1.5 row is out of
the batch at the one place that owns the flows.

Root cause 2 — ``import_commit`` counted a deselected row and a row dropped by the
narrowed re-derivation under one ``skipped`` number. The commit now returns
``skipped_rows: [{row, symbol, code, message}]`` (additive, only when non-empty).
"""

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory

D = Decimal
_HEADER = "account,symbol,side,date,shares,price\n"


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2884", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Financials", name="玉山金"))
    insert_transaction(conn, account_id="tw_broker", symbol="2884", side=Side.BUY,
                       quantity=D("100"), price=D("40"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 5))


def _preview(client: TestClient, csv_text: str) -> Any:
    return client.post("/api/import/preview",
                       json={"kind": "transactions", "csv_text": csv_text}).json()


def _commit(client: TestClient, csv_text: str, **extra: Any) -> Any:
    body: dict[str, Any] = {"kind": "transactions", "csv_text": csv_text}
    body.update(extra)
    return client.post("/api/import/commit", json=body)


def test_a_fractional_share_error_row_does_not_cover_its_sibling_sell(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """I-06 file 1: the sell must be judged against the 100 the ledger holds, not 101.5."""
    client = dashboard_client_factory(_seed)
    csv = (_HEADER + "tw_broker,2884,buy,2026-09-23,1.5,45.20\n"
           + "tw_broker,2884,sell,2026-09-23,101,45.20\n")
    rows = _preview(client, csv)["rows"]
    assert rows[0]["status"] == "error" and "整數" in rows[0]["reason"]
    assert rows[1]["status"] == "warn", rows[1]
    assert "超過持有的 100 股" in rows[1]["reason"], rows[1]["reason"]


def test_the_shortfall_sentence_quotes_whole_shares_only(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """I-06 file 2: 「買 100 ＋ 買 0.5（錯誤）＋ 賣 500」 said 「超過持有的 200.5 股」."""
    client = dashboard_client_factory(_seed)
    csv = (_HEADER + "tw_broker,2884,buy,2026-09-23,100,45.20\n"
           + "tw_broker,2884,buy,2026-09-23,0.5,45.20\n"
           + "tw_broker,2884,sell,2026-09-23,500,45.20\n")
    rows = _preview(client, csv)["rows"]
    assert rows[1]["status"] == "error"
    assert "超過持有的 200 股" in rows[2]["reason"], rows[2]["reason"]
    assert "200.5" not in rows[2]["reason"]


def test_every_skipped_row_carries_its_reason(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """I-06 file 3: buy 100 + sell 150, the buy unticked. The sell's cover vanishes with the
    tick, so it is skipped for a finding the owner never saw — and the response says so,
    per row, instead of 「跳過 2 筆」."""
    client = dashboard_client_factory(_seed)
    csv = (_HEADER + "tw_broker,2884,buy,2026-09-23,100,45.20\n"
           + "tw_broker,2884,sell,2026-09-23,150,45.20\n")
    assert [r["status"] for r in _preview(client, csv)["rows"]] == ["ok", "ok"]
    out = _commit(client, csv, select=[1], ack_warnings=False).json()
    assert out["written"] == 0 and out["skipped"] == 2
    by_row = {r["row"]: r for r in out["skipped_rows"]}
    assert by_row[1] == {"row": 1, "symbol": "2884", "code": "deselected", "message": "未勾選"}
    assert by_row[2]["code"] == "sell_exceeds_holdings"
    assert by_row[2]["symbol"] == "2884"
    assert "賣出 150 股" in by_row[2]["message"]
    assert len(client.get("/api/ledgers/transactions").json()["rows"]) == 1  # only the seed


def test_a_clean_commit_has_no_skipped_rows_field(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """ADDITIVE and only when non-empty — the byte-identical payload for every other caller."""
    client = dashboard_client_factory(_seed)
    out = _commit(client, _HEADER + "tw_broker,2884,buy,2026-09-23,10,45.20\n").json()
    assert out["written"] == 1 and out["skipped"] == 0
    assert "skipped_rows" not in out


def test_the_error_row_is_rejected_and_the_acked_sell_writes(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """With the oversell acknowledged the sell writes as the owner's informed choice; the
    1.5 row is REFUSED (``rejected_rows``), and nothing lands in ``skipped``."""
    client = dashboard_client_factory(_seed)
    csv = (_HEADER + "tw_broker,2884,buy,2026-09-23,1.5,45.20\n"
           + "tw_broker,2884,sell,2026-09-23,101,45.20\n")
    out = _commit(client, csv, ack_warnings=True).json()
    assert out["written"] == 1 and out["rejected"] == 1 and out["skipped"] == 0
    assert out["rejected_rows"][0]["row"] == 1
    assert "skipped_rows" not in out
