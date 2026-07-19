import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.csv_import import build_transaction_preview
from portfolio_dash.data_ingestion.manual import enter_transaction
from portfolio_dash.data_ingestion.resolve import ResolutionStatus, resolve
from portfolio_dash.data_ingestion.store import get_instrument, upsert_instrument
from portfolio_dash.data_ingestion.validate import TxnInput
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

_TSMC = Instrument(
    symbol="2330",
    market=Market.TW,
    quote_ccy=Currency.TWD,
    sector="Tech",
    name="台積電",
)
# 2303 聯華電子 (UMC) — the REAL company the live bug coerced into 2330 台積電.
_UMC = Instrument(
    symbol="2303",
    market=Market.TW,
    quote_ccy=Currency.TWD,
    sector="Tech",
    name="聯華電子",
)


def test_instrument_roundtrip(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, _TSMC)
    upsert_instrument(conn, _TSMC)  # idempotent
    got = get_instrument(conn, "2330")
    assert got is not None and got.name == "台積電" and got.market is Market.TW


def test_resolve_exact_symbol(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, _TSMC)
    r = resolve(conn, "2330")
    assert r.status is ResolutionStatus.EXACT
    assert r.instrument is not None and r.instrument.symbol == "2330"


def test_resolve_needs_ai_when_unknown(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, _TSMC)
    r = resolve(conn, "ZZ Unknown Corp")
    assert r.status is ResolutionStatus.NEEDS_AI and r.instrument is None


# --- R6-A regression: code-shaped input is EXACT-only, NEVER coerced ---------------------
# The live bug: with only 2330 registered, "2303" fuzzy-matched to 2330 (SequenceMatcher
# ratio == 0.75) and was rewritten with a 「視為」 confirmation. Digit edit-distance has no
# meaning for exchange codes, so an unregistered code now routes to register-first with NO
# candidates (never a near-miss code suggestion).


def test_resolve_near_miss_code_does_not_coerce_2303(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, _TSMC)  # ONLY 2330 registered
    r = resolve(conn, "2303")
    assert r.status is ResolutionStatus.NEEDS_AI
    assert r.instrument is None
    assert r.candidates == []  # code shape -> never a near-miss code suggestion


def test_resolve_near_miss_code_does_not_coerce_2883(conn: sqlite3.Connection) -> None:
    # The second real case: 2883 開發金 was coerced to 2882 國泰金 (also a 0.75 tie).
    upsert_instrument(conn, Instrument(symbol="2882", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Fin", name="國泰金"))
    r = resolve(conn, "2883")
    assert r.status is ResolutionStatus.NEEDS_AI
    assert r.instrument is None and r.candidates == []


def test_resolve_name_shape_offers_nonbinding_suggestions(conn: sqlite3.Connection) -> None:
    # A NAME-shaped input (聯電) may earn non-binding NAME suggestions, but never binds:
    # 聯電 vs 聯華電子 scores ~0.67 (>= 0.6), so 2303 is offered as a hint; instrument stays None.
    upsert_instrument(conn, _UMC)
    r = resolve(conn, "聯電")
    assert r.status is ResolutionStatus.NEEDS_AI
    assert r.instrument is None  # non-binding — the caller must still register/confirm
    assert any(c.symbol == "2303" for c in r.candidates)


def test_resolve_name_shape_below_threshold_has_no_suggestions(
    conn: sqlite3.Connection,
) -> None:
    # 聯電 vs 台積電 scores 0.4 (< 0.6) -> no suggestion; NEEDS_AI with empty candidates.
    upsert_instrument(conn, _TSMC)
    r = resolve(conn, "聯電")
    assert r.status is ResolutionStatus.NEEDS_AI and r.candidates == []


# --- entry-path regression: the draft carries a hard block, no 「視為」 anywhere -----------


def _setup_accounts(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, _TSMC)  # ONLY 2330 registered


def _no_coercion_wording(messages: list[str]) -> None:
    joined = " ".join(messages)
    assert "視為" not in joined and "模糊" not in joined


def test_manual_entry_near_miss_code_is_hard_unresolved(conn: sqlite3.Connection) -> None:
    _setup_accounts(conn)
    inp = TxnInput(account_id="tw_broker", symbol="2303", side=Side.BUY,
                   quantity=Decimal("1000"), price=Decimal("50"),
                   trade_date=date(2026, 6, 1))
    draft = enter_transaction(conn, inp, confirm=True)  # confirm must NOT bypass a hard block
    assert draft.written is False
    unresolved = [i for i in draft.issues if i.kind == "symbol_unresolved"]
    assert len(unresolved) == 1 and unresolved[0].needs_confirm is False
    assert not any(i.kind == "fuzzy_resolved" for i in draft.issues)
    _no_coercion_wording([i.message for i in draft.issues])


def test_manual_entry_name_shape_appends_suggestion_tail(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, _UMC)  # 2303 聯華電子 registered; 聯電 is a name-shaped miss
    inp = TxnInput(account_id="tw_broker", symbol="聯電", side=Side.BUY,
                   quantity=Decimal("1000"), price=Decimal("50"),
                   trade_date=date(2026, 6, 1))
    draft = enter_transaction(conn, inp, confirm=False)
    unresolved = next(i for i in draft.issues if i.kind == "symbol_unresolved")
    # non-binding hint appears in the message, but the symbol is not bound / rewritten
    assert "相近名稱" in unresolved.message and "2303" in unresolved.message
    assert "視為" not in unresolved.message
    assert draft.instrument is None


def test_csv_import_near_miss_code_not_coerced(conn: sqlite3.Connection) -> None:
    _setup_accounts(conn)
    csv_text = (
        "account,symbol,side,date,shares,price\n"
        "tw_broker,2303,buy,2026-06-02,1000,50\n"
    )
    preview = build_transaction_preview(conn, csv_text)
    row = preview.rows[0]
    assert any(i.kind == "symbol_unresolved" for i in row.issues)
    assert not any(i.kind == "fuzzy_resolved" for i in row.issues)
    assert row.payload["symbol"] == "2303"  # raw kept, NOT coerced to 2330
    _no_coercion_wording([i.message for i in row.issues])
