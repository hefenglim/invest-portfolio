from fastapi.testclient import TestClient


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
    assert b["holdings"]["tw_broker"]["2330"] == "1000"
    assert b["holdings"]["schwab"]["AAPL"] == "10"
    # Batch B (additive): each account carries a per-market ``markets`` bundle. Every current
    # account is single-market, so it mirrors the legacy scalar div_model + fee_rules (the
    # merged-account shape is contract-tested via the shared helper in test_accounts_api.py).
    tw = accts["tw_broker"]
    assert set(tw["markets"]) == {"TW"}
    assert tw["markets"]["TW"]["div_model"] == "tw"
    assert tw["markets"]["TW"]["fee_rules"] == b["fee_rules"]["tw_broker"]
