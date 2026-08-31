"""FIX-A1 (QA-01 extended to transactions): the commit's select re-derives the sibling batch.

**Measured defect (Master probe T2, 2026-08-29).** ``build_transaction_preview`` is
sibling-aware — a sell covered by a buy earlier in the same file previews clean — but the
commit intersected ``select`` and re-derived only for ``cash``/``fx``. Deselect the covering
buy and the sell's clean verdict SURVIVED: ``select=[sell]`` answered 200 / ``written: 1`` /
holdings ``shares: "-60"`` — a silently written oversold state whose 賣超 confirmation (the
one ack that permanently discards a cost basis, domain-ledger.md's STICKY rule) was never
shown, while the manual door answers the identical sell with the sticky needs_confirm.

The fix mirrors the QA-01 cash/fx block with one adaptation: those guards reject HARD, so
re-deriving over ``accept`` drops what they refuse; the transactions guard raises SOFT
findings that ride the blanket ``ack_warnings`` — an ack given against the FULL-file
preview. A row whose narrowed verdict surfaces an issue KIND absent from its full-file
verdict is therefore dropped into ``skipped`` (never written under a confirmation the owner
never saw), while a row whose kinds are unchanged still writes — that ack was informed.
"""

import sqlite3
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import DashboardClientFactory

_HEADER = "account,symbol,side,date,shares,price\n"

#: The Master-probe file: a buy and the sell it alone covers.
_COVERED_SELL = (
    _HEADER
    + "schwab,AAPL,buy,2025-01-10,100,50.00\n"   # 0: covering buy
    + "schwab,AAPL,sell,2025-02-10,60,55.00\n"   # 1: covered ONLY by row 0
)


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="Apple"))
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Semiconductors",
                                       name="TSMC"))


def _commit(client: TestClient, csv_text: str, **extra: Any) -> Any:
    body: dict[str, Any] = {"kind": "transactions", "csv_text": csv_text}
    body.update(extra)
    return client.post("/api/import/commit", json=body)


def _txn_rows(client: TestClient) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = client.get("/api/ledgers/transactions").json()["rows"]
    return rows


def test_the_full_file_commits_with_zero_flags(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Probe T1's pin: the sibling-aware preview is intact — the covered sell carries no
    warning at all, so the whole file commits without any ``ack_warnings``."""
    client = dashboard_client_factory(_seed)
    preview = client.post("/api/import/preview",
                          json={"kind": "transactions", "csv_text": _COVERED_SELL}).json()
    assert [r["status"] for r in preview["rows"]] == ["ok", "ok"]
    response = _commit(client, _COVERED_SELL, ack_warnings=False)
    assert response.status_code == 200, response.text
    assert response.json()["written"] == 2


def test_a_deselected_covering_buy_cannot_fund_the_sell_written_alone(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The T2 reproduction: commit ONLY the sell. It must land in ``skipped`` — not in the
    ledger as a silent −60, and not in ``rejected`` (nothing refused it; its one soft
    confirmation was simply never shown)."""
    client = dashboard_client_factory(_seed)
    response = _commit(client, _COVERED_SELL, select=[1], ack_warnings=False)
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["written"] == 0, f"wrote a silently-oversold sell: {out}"
    assert out["skipped"] == 2
    assert "rejected" not in out
    assert _txn_rows(client) == [], "the ledger holds a sell nothing covers"


def test_the_blanket_ack_does_not_cover_a_finding_the_owner_never_saw(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """``ack_warnings=True`` acknowledges the FULL-file preview — which showed nothing.
    The 賣超 that only exists under the narrowed batch was never on any screen, so the
    blanket ack must not write it (kind-level shrink rule)."""
    client = dashboard_client_factory(_seed)
    out = _commit(client, _COVERED_SELL, select=[1], ack_warnings=True).json()
    assert out["written"] == 0 and out["skipped"] == 2
    assert _txn_rows(client) == []


def test_deselecting_an_unrelated_row_keeps_the_sells_cover(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The narrowing is exact: dropping a row of a DIFFERENT position must not shrink the
    batch the sell is judged against — buy + sell still write as a pair."""
    client = dashboard_client_factory(_seed)
    csv_text = _COVERED_SELL + "tw_broker,2330,buy,2025-03-03,10,500\n"  # 2: unrelated
    response = _commit(client, csv_text, select=[0, 1], ack_warnings=False)
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["written"] == 2 and out["skipped"] == 1
    symbols = {r["symbol"] for r in _txn_rows(client)}
    assert symbols == {"AAPL"}


#: FIX-A1b (Master probe V6): the covering buy is STRUCTURALLY invalid — a hard error on
#: its own line, so it can never be written and must never cover.
_BROKEN_COVER = (
    _HEADER
    + "schwab,AAPL,buy,2025-01-10,100,-50.00\n"  # 0: hard non_positive_price
    + "schwab,AAPL,sell,2025-02-10,60,55.00\n"   # 1: "covered" only by the un-writable buy
)


def test_v6_a_structurally_invalid_buy_cannot_silently_fund_the_sell(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The V6 reproduction, no select: the sell must surface the 賣超 needs_confirm at
    PREVIEW (the typo'd buy no longer counts as cover), the unacked commit must refuse,
    and the acked commit writes an oversell the owner has actually been shown — never the
    silent lone SELL of the probe (200 / written 1 / unacked oversold)."""
    client = dashboard_client_factory(_seed)
    preview = client.post("/api/import/preview",
                          json={"kind": "transactions", "csv_text": _BROKEN_COVER}).json()
    assert [r["status"] for r in preview["rows"]] == ["error", "warn"]
    assert "超過" in preview["rows"][1]["reason"] and "持有" in preview["rows"][1]["reason"]
    refused = _commit(client, _BROKEN_COVER, ack_warnings=False)
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "warnings_unacknowledged"
    assert _txn_rows(client) == []
    acked = _commit(client, _BROKEN_COVER, ack_warnings=True).json()
    assert acked["written"] == 1 and acked.get("rejected") == 1  # informed ack + loud buy
    assert acked["rejected_rows"][0]["kind"] == "non_positive_price"


def test_v6_with_a_selection_gets_the_same_protection(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The FIX-A1 narrowing composes on top: with ``select=[sell]`` the verdict is the
    same — refuse unacked, write only an INFORMED acked oversell (the flag was on the
    full-file preview, so its kind is unchanged under the narrowed batch)."""
    client = dashboard_client_factory(_seed)
    refused = _commit(client, _BROKEN_COVER, select=[1], ack_warnings=False)
    assert refused.status_code == 422, refused.text
    assert _txn_rows(client) == []
    acked = _commit(client, _BROKEN_COVER, select=[1], ack_warnings=True).json()
    assert acked["written"] == 1 and acked.get("rejected") == 1
    assert [r["symbol"] for r in _txn_rows(client)] == ["AAPL"]


def test_v7_cash_parity_the_bad_deposit_still_cannot_fund_the_withdrawal(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The boundary being mirrored, pinned on the door that always had it: a hard-invalid
    deposit (−500) is out of the cash batch, so the withdrawal it "funded" stays
    hard-blocked at the guard and nothing writes."""
    client = dashboard_client_factory(_seed)
    csv_text = ("account,date,kind,ccy,amount,acq_home_amount,note\n"
                "tw_broker,2026-01-01,deposit,TWD,-500,,\n"
                "tw_broker,2026-02-01,withdraw,TWD,300,,\n")
    response = client.post("/api/import/commit", json={
        "kind": "cash", "csv_text": csv_text, "ack_warnings": True})
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["written"] == 0 and out.get("rejected") == 2
    kinds = {r["kind"] for r in out["rejected_rows"]}
    assert "withdraw_insufficient_balance" in kinds
    balances = client.get("/api/cash").json()["balances"]
    assert all(row["amount"] == "0" for row in balances
               if row["account_id"] == "tw_broker" and row["ccy"] == "TWD")


def test_an_acked_genuine_oversell_still_writes_under_narrowing(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The other half of the kind-level rule: a sell that was ALREADY flagged 賣超 on the
    full-file preview, and acked, keeps writing when a selection narrows the batch — its
    issue kinds are unchanged, so that ack was informed. Only the numbers in the message
    can differ under a smaller batch, and numbers are not a new confirmation."""
    client = dashboard_client_factory(_seed)
    csv_text = (
        _HEADER
        + "schwab,AAPL,sell,2025-02-10,60,55.00\n"   # 0: genuinely oversold, nothing covers it
        + "tw_broker,2330,buy,2025-03-03,10,500\n"   # 1: unrelated, deselected
    )
    preview = client.post("/api/import/preview",
                          json={"kind": "transactions", "csv_text": csv_text}).json()
    assert preview["rows"][0]["status"] == "warn"    # the ack below is informed
    response = _commit(client, csv_text, select=[0], ack_warnings=True)
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["written"] == 1 and out["skipped"] == 1
    assert [r["symbol"] for r in _txn_rows(client)] == ["AAPL"]
