"""Item 8 (owner ruling 2026-09-30): a card pairs every registered code with one of that
instrument's names — its registered name or an alias — or the card is not stored.

The owner: 「確保正式上線 prod 全新網站的時候，新的名稱代號確保正確不會再錯誤即可，舊有資料就
進行修正」, and chose 「登錄名稱＋中文別名」. Measured on the demo's 224 cards before the rule:
10 cards held 26 wrong pairings — 「聯詠 (3008)」 (3008 is 大立光), 「台積電 (3008)」, 「陽明
(2603)」 (陽明 is 2609), 「長榮航 (2603)」, 「聯發科技（2412）」 (聯發科技 is 2454), 「LARGNA
(3008)」, 「2412（未命名）」 — while 「大立光 (3008)」 beside a code registered as LARGAN was right.
Nothing checked a pairing: DEF-082's figure check asks only whether a code exists.

The shapes below are the demo's own, verbatim where they were measured.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.data_ingestion.store import set_instrument_aliases, upsert_instrument
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import generate
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.llm_insight.generate import RunInputs
from portfolio_dash.llm_insight.name_check import (
    feedback_lines,
    mismatches,
    naming_table,
    registry_from_db,
)
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.instrument_names import NamedInstrument
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    add_topup,
    ensure_llm_seeded,
    set_role,
    upsert_model,
)
from portfolio_dash.shared.models.assets import Instrument

NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))

REG = [
    NamedInstrument("3008", "LARGAN", ("大立光",)),
    NamedInstrument("2330", "台積電", ("TSMC",)),
    NamedInstrument("2603", "Evergreen", ("長榮", "長榮海運")),
    NamedInstrument("2609", "Yang Ming", ("陽明", "陽明海運")),
    NamedInstrument("2412", "中華電信", ("中華電",)),
    NamedInstrument("2454", "聯發科", ("聯發科技",)),
    NamedInstrument("2884", "玉山金控", ()),
    NamedInstrument("2317", "鴻海", ()),
    NamedInstrument("2323", "中環", ()),
    NamedInstrument("5225", "IHH Healthcare", ()),
    NamedInstrument("AAPL", "Apple", ("蘋果",)),
]


@pytest.mark.parametrize("text", [
    "大立光 (3008) 權重偏高",                    # an alias
    "LARGAN（3008）的未實現損益",                 # the registered name, full-width brackets
    "3008 (LARGAN) 與 5225 (IHH) 同列",           # code first; a word of a Latin name
    "近期無關於玉山金 (2884) 的新聞",              # a leading part of a Chinese name
    "您持有鴻海 (2317) 1,000 股",                 # words before the name
    "該標的 (2323) 已暫停交易",                   # a generic referent
    "收盤 215.3 USD (AAPL)",                      # a unit, not a name
    "台股現貨資料 (2330) 過期",                   # a generic noun
    "AAPL (基準日2026-07-02) 的走勢",             # a note in the brackets, not a name
    "代號如下：(2330)",                           # nothing names a company
    "2330（台積電）與 AAPL (蘋果)",
    "3008 權重過高警示",                          # a bare code, then words that name nothing
    "2317 鴻海：持股健檢",                        # a bare code, then its own name
    "2330 與 3008 LARGAN 同列",
])
def test_a_right_pairing_passes(text: str) -> None:
    assert mismatches(text, REG) == []


@pytest.mark.parametrize("text, code, written, belongs_to, exact", [
    ("台股部位週報：聯詠 (3008) 貢獻最大", "3008", "聯詠", None, False),
    ("台積電(3008) 偏多持股健檢", "3008", "台積電", "2330", True),
    ("陽明（2603）股價處於多頭格局", "2603", "陽明", "2609", True),
    ("長榮航 (2603) 區間震盪", "2603", "長榮航", None, False),
    ("**聯發科技（2412）**：1,000 股", "2412", "聯發科技", "2454", True),
    ("主要由台積電（2330）與 LARGNA（3008）貢獻", "3008", "LARGNA", None, False),
    ("| 2412（聯詠） | 2026-07-09 |", "2412", "聯詠", None, True),
    ("| 2412（未命名） | 2412 |", "2412", "未命名", None, True),
    # a bare code in a title, followed by another registered instrument's alias (#113)
    ("2603 陽明：多方格局，留意高檔震盪", "2603", "陽明", "2609", True),
])
def test_a_wrong_pairing_is_found(text: str, code: str, written: str, belongs_to: str | None,
                                  exact: bool) -> None:
    [mm] = mismatches(text, REG)
    assert (mm.code, mm.written, mm.belongs_to, mm.span is not None) == (
        code, written, belongs_to, exact)
    if exact:
        assert mm.span is not None and text[mm.span[0]:mm.span[1]] == written


def test_an_unregistered_code_is_not_this_checks_business() -> None:
    """「LRDIM (6883)」 — 6883 is not registered: the DEF-082 figure check flags it."""
    assert mismatches("主要動能來自 LRDIM (6883)", REG) == []


def test_the_retry_note_names_the_right_names() -> None:
    lines = feedback_lines(mismatches("台積電(3008) 偏多", REG))
    assert lines == "・『台積電（3008）』——3008 的名稱是 LARGAN、大立光；台積電 是 2330"


def test_the_prompt_table_lists_only_the_codes_the_prompt_mentions() -> None:
    """A per-market card's input holds no other market's symbols (the isolation guard)."""
    table = naming_table(REG, within="持倉：2330 權重 60%、3008 權重 20%")
    assert table.splitlines() == ["2330：台積電、TSMC", "3008：LARGAN、大立光"]


# --- the generation seam ------------------------------------------------------------------

def _card(body: str) -> str:
    return ('{"title":"週報","summary":"持股檢視","body_md":"' + body + '","tags":["TW"],'
            '"symbol":null,"confidence":60,"prediction":null}')


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [type("M", (), {"message": type("X", (), {"content": content})()})()]
        self.usage = type("U", (), {"prompt_tokens": 100, "completion_tokens": 20})()


@pytest.fixture
def conn(golden_db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    cs.ensure_seeded(golden_db)
    istore.ensure_tables(golden_db)
    ensure_llm_seeded(golden_db)
    upsert_model(golden_db, ModelConfig(
        id="m", model_alias="m", provider="openai", model_name="m", max_retries=0,
        input_price_per_mtok=Decimal("1"), output_price_per_mtok=Decimal("2"),
    ))
    set_role(golden_db, LLMRole.DEFAULT, "m")
    add_topup(golden_db, Decimal("100"))
    upsert_instrument(golden_db, Instrument(symbol="3008", market=Market.TW,
                                            quote_ccy=Currency.TWD, sector="Tech",
                                            name="LARGAN", board="TWSE"))
    set_instrument_aliases(golden_db, "3008", ["大立光"])
    yield golden_db


def _run(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
         replies: list[str]) -> tuple[generate.RunResult, list[str], int]:
    sent: list[str] = []

    def completion(**kw: object) -> _Resp:
        msgs = kw["messages"]
        assert isinstance(msgs, list)
        sent.append(str(msgs[-1]["content"]))
        return _Resp(replies.pop(0))

    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: False)
    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    sp = cs.create_strategy(conn, name="S", body="持倉：{{holdings_json}}", now=NOW)
    it = cs.create_insight_type(conn, name="Daily", scope="portfolio", now=NOW)
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    data = build_dashboard(conn, now=NOW, reporting=Currency.TWD)
    result = generate.run_insight_type(
        conn, it.id, var_contexts={None: V.VarContext(data=data, now=NOW, symbol=None)},
        inputs=RunInputs(budget_remaining=Decimal("100")), now=NOW)
    return result, sent, it.id


def test_the_prompt_carries_the_names_of_what_it_mentions(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _result, sent, _ = _run(conn, monkeypatch, [_card("2330 (TSMC) 權重最高")])
    assert "[標的名稱守則]" in sent[0] and "2330：TSMC" in sent[0]


def test_a_misnamed_reply_is_asked_again_and_the_fix_is_stored(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, sent, it_id = _run(conn, monkeypatch, [
        _card("主要由 聯詠 (3008) 貢獻"), _card("主要由 大立光 (3008) 貢獻")])
    assert len(sent) == 2
    assert "[名稱修正]" in sent[1] and "『聯詠（3008）』——3008 的名稱是 LARGAN、大立光" in sent[1]
    [card] = istore.list_cards(conn, insight_type_id=it_id)
    assert "大立光 (3008)" in card.card.body_md and "聯詠" not in card.card.body_md
    assert result.status == "ok"


def test_a_reply_still_misnamed_is_not_stored(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """寧缺勿錯 — and the run says so, and both calls are on its bill."""
    result, sent, it_id = _run(conn, monkeypatch, [
        _card("主要由 聯詠 (3008) 貢獻"), _card("主要由 台積電 (3008) 貢獻")])
    assert len(sent) == 2
    assert istore.list_cards(conn, insight_type_id=it_id) == []
    row = conn.execute("SELECT status, reason, detail, llm_calls FROM job_runs WHERE job_id=?",
                       (f"insight:{it_id}",)).fetchone()
    assert (row["status"], row["reason"], row["llm_calls"]) == ("partial", "name_mismatch", 2)
    assert row["detail"].endswith("；1 張因名稱與代號不符未存")
    assert result.cards_created == 0


def test_the_registry_is_read_with_its_aliases(conn: sqlite3.Connection) -> None:
    by = {i.symbol: i for i in registry_from_db(conn)}
    assert by["3008"].names == ("LARGAN", "大立光")


def test_a_database_without_the_aliases_column_reads_names_only() -> None:
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE instruments (symbol TEXT, name TEXT)")
    c.execute("INSERT INTO instruments VALUES ('2330', '台積電')")
    assert registry_from_db(c) == [NamedInstrument("2330", "台積電", ())]
    assert registry_from_db(sqlite3.connect(":memory:")) == []
