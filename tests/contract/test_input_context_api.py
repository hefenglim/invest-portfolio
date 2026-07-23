import sqlite3

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import set_instrument_archived, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument


def test_input_context_shape(api_client: TestClient) -> None:
    r = api_client.get("/api/input/context")
    assert r.status_code == 200
    b = r.json()
    accts = {a["id"]: a for a in b["accounts"]}
    assert accts["tw_broker"]["div_model"] == "tw" and accts["tw_broker"]["ccy"] == "TWD"
    assert accts["schwab"]["div_model"] == "drip"
    # Merged Moomoo: scalar div_model is the US pair (drip); its MY market books single-tier net.
    assert accts["moomoo_my"]["div_model"] == "drip"
    assert accts["moomoo_my"]["markets"]["MY"]["div_model"] == "net"
    fr = b["fee_rules"]["tw_broker"]
    assert fr["rate"] == "0.001425" and fr["min_fee"] == "20" and fr["round_int"] is True
    assert fr["tax_sell"] == "0.003" and "label" in fr
    insts = {i["symbol"]: i for i in b["instruments"]}
    assert insts["2330"]["etf"] is False and insts["2330"]["ccy"] == "TWD"
    # Fable F7: every instrument carries an `archived` flag so the shared 代號 picker can
    # exclude archived symbols from its 未持有 group. A live symbol is never archived.
    assert insts["2330"]["archived"] is False
    assert b["holdings"]["tw_broker"]["2330"] == "1000"
    assert b["holdings"]["schwab"]["AAPL"] == "10"
    # Batch B (additive): each account carries a per-market ``markets`` bundle. Every current
    # account is single-market, so it mirrors the legacy scalar div_model + fee_rules (the
    # merged-account shape is contract-tested via the shared helper in test_accounts_api.py).
    tw = accts["tw_broker"]
    assert set(tw["markets"]) == {"TW"}
    assert tw["markets"]["TW"]["div_model"] == "tw"
    assert tw["markets"]["TW"]["fee_rules"] == b["fee_rules"]["tw_broker"]


def test_input_context_marks_archived_instruments(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Fable F7: an archived instrument is still serialized (so a held position can resolve its
    name) but flagged ``archived: true`` — the shared picker uses the flag to keep it out of the
    未持有 candidate list (a stealth 缺價 risk)."""
    upsert_instrument(golden_db, Instrument(symbol="9999", market=Market.TW,
                                            quote_ccy=Currency.TWD, sector="Electronics",
                                            name="Ghost Co", board="TWSE"))
    set_instrument_archived(golden_db, "9999", True)
    golden_db.commit()
    b = api_client.get("/api/input/context").json()
    insts = {i["symbol"]: i for i in b["instruments"]}
    assert "9999" in insts and insts["9999"]["archived"] is True
    assert insts["2330"]["archived"] is False
