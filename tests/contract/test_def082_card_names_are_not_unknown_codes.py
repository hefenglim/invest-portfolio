"""Contract — DEF-082 (functional-test R8, 2026-09-27): a registered name is not a code.

Card #209 on the demo wrote 「3008 (LARGAN)」. LARGAN is 3008's registered name, and the
card wore 「未知代碼」 with the tooltip 「…可能是模型幻覺」, because the M9 symbol check
compared a parenthesised token against registered SYMBOLS only. Through the real route:
the registry's names clear 「代號 (英文名)」 in any case, whole or word by word (owner ruling
2026-09-27), and a code no symbol or name accounts for — #82's 「LRDIM (6883)」 — is still
flagged, in the flat list and the grouped list alike.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight.cards import InsightCard
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument


def _task(api_client: TestClient) -> int:
    sp = api_client.post(
        "/api/strategy-prompts", json={"name": "S", "body": "{{kpis_json}}"}
    ).json()
    it = api_client.post(
        "/api/insight-types",
        json={"name": "週報", "scope": "per_symbol", "strategy_ids": [sp["id"]]},
    ).json()
    return int(it["id"])


def _flags(api_client: TestClient, golden_db: sqlite3.Connection) -> dict[str, list[str]]:
    """Store three cards and read each one's ``unknown_symbols`` back off both list shapes."""
    upsert_instrument(golden_db, Instrument(symbol="3008", market=Market.TW,
                                            quote_ccy=Currency.TWD, sector="Tech",
                                            name="LARGAN", board="TWSE"))
    upsert_instrument(golden_db, Instrument(symbol="5225", market=Market.MY,
                                            quote_ccy=Currency.MYR, sector="Health Care",
                                            name="IHH Healthcare", board=".KL"))
    it_id = _task(api_client)
    now = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))
    cards = {
        "3008": "台股部位週報：3008 (LARGAN) 權重偏高。",
        "5225": "5225 (IHH) 與 AAPL (APPLE)、2330（TSMC）同列觀察。",
        "2330": "建議留意 LRDIM (6883) 的評價。",
    }
    for symbol, body in cards.items():
        istore.add_card(
            golden_db, insight_type_id=it_id,
            card=InsightCard(title=f"{symbol} 週報", summary="部位檢視", body_md=body,
                             symbol=symbol),
            fingerprint=istore.fingerprint(it_id, symbol, "d", "v1"),
            calibration_version=None, horizon_days=5, input_snapshot="", model="m",
            cost_usd=Decimal("0"), now=now,
        )
    flat = {r["symbol"]: r["figure_flags"]["unknown_symbols"]
            for r in api_client.get("/api/insights").json()["rows"]}
    grouped = {g["symbol"]: g["cards"][0]["figure_flags"]["unknown_symbols"]
               for g in api_client.get("/api/insights?group=symbol").json()["groups"]}
    assert flat == grouped
    return flat


def test_code_then_english_name_is_not_an_unknown_code(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The #209 shape: 「3008 (LARGAN)」 with LARGAN registered as 3008's name."""
    assert _flags(api_client, golden_db)["3008"] == []


def test_a_name_clears_in_any_case_and_by_word(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """「(IHH)」 is a word of ``IHH Healthcare``; 「(APPLE)」 and 「（TSMC）」 are the golden
    ledger's ``Apple`` / ``TSMC`` — one only once upper-cased, one in full-width brackets."""
    assert _flags(api_client, golden_db)["5225"] == []


def test_a_real_unknown_code_is_still_flagged(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """#82's 「LRDIM (6883)」: no symbol and no name accounts for 6883."""
    assert _flags(api_client, golden_db)["2330"] == ["6883"]
