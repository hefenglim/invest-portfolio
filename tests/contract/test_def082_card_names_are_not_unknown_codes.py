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


# --- the four boundaries R9's verifier mutated without a test failing (owner 2026-09-30) ----
# Each behaviour below was black-box verified correct in R9 and pinned by nothing: the
# verifier changed it and every test stayed green. Each test is the mutation it pins.

def _unknown(api_client: TestClient, golden_db: sqlite3.Connection, body: str,
             *registered: Instrument, archive: str | None = None) -> list[str]:
    """Register *registered*, optionally archive one, store ONE card, read its flags back."""
    for inst in registered:
        upsert_instrument(golden_db, inst)
    if archive is not None:
        golden_db.execute("UPDATE instruments SET archived = 1 WHERE symbol = ?", (archive,))
        golden_db.commit()
    it_id = _task(api_client)
    istore.add_card(
        golden_db, insight_type_id=it_id,
        card=InsightCard(title="邊界檢核", summary="部位檢視", body_md=body, symbol="2330"),
        fingerprint=istore.fingerprint(it_id, body, "d", "v1"),
        calibration_version=None, horizon_days=5, input_snapshot="", model="m",
        cost_usd=Decimal("0"), now=datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei")),
    )
    [row] = [r for r in api_client.get("/api/insights").json()["rows"]
             if r["title"] == "邊界檢核"]
    return list(row["figure_flags"]["unknown_symbols"])


def _tw(symbol: str, name: str) -> Instrument:
    return Instrument(symbol=symbol, market=Market.TW, quote_ccy=Currency.TWD,
                      sector="Tech", name=name, board="TWSE")


def test_an_archived_instruments_name_still_clears(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Mutation ⑤ (archived names left out): a card may still name a company the ledger
    held and archived — the registry knows it, so it is not an invented code."""
    assert _unknown(api_client, golden_db, "持倉回顧：9901 (ZEBRAX) 已出清。",
                    _tw("9901", "ZEBRAX Corp"), archive="9901") == []


def test_a_two_letter_word_of_a_name_clears(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Mutation ⑥ (two-letter words dropped): 「(LG)」 for ``LG Electronics``. Only ONE
    letter is never a name form (F / T / X are US tickers)."""
    assert _unknown(api_client, golden_db, "同業比較：9902 (LG) 營收持平。",
                    _tw("9902", "LG Electronics")) == []


def test_a_letter_and_digit_word_clears_whole(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Mutation ⑦ (a word read as letters only, ALPHA2 → ALPHA): the word is the name's own
    spelling, digits included, or the card's 「(ALPHA2)」 reads as an invented code."""
    assert _unknown(api_client, golden_db, "基金觀察：9903 (ALPHA2) 淨值回升。",
                    _tw("9903", "ALPHA2 Holdings")) == []


def test_a_name_is_matched_whole_never_by_prefix(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Mutation ⑧ (prefix match): 「(LARGA)」 is not ``LARGAN`` — a prefix match would clear
    any truncated or invented code that happens to start a registered name."""
    assert _unknown(api_client, golden_db, "台股部位週報：3008 (LARGA) 權重偏高。",
                    _tw("3008", "LARGAN")) == ["LARGA"]
