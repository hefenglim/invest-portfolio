"""Options W3 — the transaction validators (spec §4.2, plan §4).

Every rule a door shares: the structural price-0 gate, the close row's fee refusal, the
derived side / quantity / date of a close row, the after-expiry and same-day-after-expiry
rules, the stored-``expired``-row protection, multiples of 100, the CSV / edit over-cover
warning, and the option refusals elsewhere (dividend, opening, the equity corporate actions).

Hermetic: an in-memory ledger, written through the store. None of these tests replays an
``OPTION_ADJUST`` or a §4.4-refused close — both belong to the replay (W2) and are crossed at
the wave-1 merge.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import (
    get_transaction,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.data_ingestion.validate import (
    CorporateActionInput,
    DividendInput,
    Issue,
    TxnInput,
    option_close_issues,
    transaction_structural_issues,
    validate_corporate_action,
    validate_dividend,
    validate_transaction,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

PUT = "XYZ261218P00050000"      # XYZ 2026-12-18 50 Put
CALL = "XYZ261218C00060000"
EXPIRY = date(2026, 12, 18)
STRANDED = "此合約已登錄到期作廢，請先修改或刪除該列"


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    seed_accounts(c)
    upsert_instrument(c, Instrument(symbol="XYZ", market=Market.US, quote_ccy=Currency.USD,
                                    sector="Tech", name="XYZ Corp"))
    upsert_instrument(c, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                    sector="Semis", name="TSMC"))
    register_option_contract(c, PUT)
    register_option_contract(c, CALL)
    return c


def _tx(conn: sqlite3.Connection, side: Side, qty: str, d: date, *, symbol: str = PUT,
        price: str = "2", short: bool = False, close: str | None = None,
        account: str = "schwab") -> int:
    return insert_transaction(
        conn, account_id=account, symbol=symbol, side=side, quantity=Decimal(qty),
        price=Decimal(price), fees=Decimal("0"), tax=Decimal("0"), trade_date=d,
        short_sale=short, close_reason=close)  # type: ignore[arg-type]


def _inp(side: Side, qty: str, d: date, *, symbol: str = PUT, price: str = "0",
         close: str | None = None, short: bool = False, fee: str | None = None,
         tax: str | None = None, account: str = "schwab") -> TxnInput:
    return TxnInput(account_id=account, symbol=symbol, side=side, quantity=Decimal(qty),
                    price=Decimal(price), trade_date=d, short_sale=short,
                    close_reason=close,  # type: ignore[arg-type]
                    fee=None if fee is None else Decimal(fee),
                    tax=None if tax is None else Decimal(tax))


def _kinds(issues: list[Issue]) -> set[str]:
    return {i.kind for i in issues}


def _hard(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if not i.needs_confirm]


# --- structural ----------------------------------------------------------------------------


def test_a_price_zero_row_without_a_close_reason_keeps_its_refusal() -> None:
    issues = transaction_structural_issues(_inp(Side.BUY, "100", EXPIRY))
    assert [i.message for i in issues] == ["價格必須大於 0"]


def test_a_price_zero_close_row_is_structurally_clean() -> None:
    assert transaction_structural_issues(
        _inp(Side.BUY, "100", EXPIRY, close="expired")) == []


def test_a_close_row_must_be_priced_zero_and_undeclared() -> None:
    priced = transaction_structural_issues(
        _inp(Side.BUY, "100", EXPIRY, close="expired", price="1"))
    assert _kinds(priced) == {"close_row_price"}
    short = transaction_structural_issues(
        _inp(Side.SELL, "100", EXPIRY, close="expired", short=True))
    assert _kinds(short) == {"close_row_short_sale"}


@pytest.mark.parametrize(("fee", "tax"), [("0.65", None), (None, "0.01"), ("1", "1")])
def test_a_close_row_refuses_a_non_zero_fee_or_tax(fee: str | None, tax: str | None) -> None:
    issues = transaction_structural_issues(
        _inp(Side.BUY, "100", EXPIRY, close="expired", fee=fee, tax=tax))
    assert [i.message for i in issues] == ["到期作廢／被指派／履約列不收手續費與稅"]


def test_a_zero_fee_on_a_close_row_is_fine() -> None:
    assert transaction_structural_issues(
        _inp(Side.BUY, "100", EXPIRY, close="expired", fee="0", tax="0")) == []


# --- close rules -----------------------------------------------------------------------------


def test_expired_on_a_short_is_a_buy_of_the_whole_position(conn: sqlite3.Connection) -> None:
    _tx(conn, Side.SELL, "200", date(2026, 11, 2), short=True)
    ok = validate_transaction(conn, _inp(Side.BUY, "200", EXPIRY, close="expired"))
    assert _hard(ok) == []
    wrong_side = validate_transaction(conn, _inp(Side.SELL, "200", EXPIRY, close="expired"))
    assert "option_close_side_mismatch" in _kinds(_hard(wrong_side))
    partial = validate_transaction(conn, _inp(Side.BUY, "100", EXPIRY, close="expired"))
    assert "option_expiry_quantity" in _kinds(_hard(partial))
    early = validate_transaction(conn, _inp(Side.BUY, "200", date(2026, 12, 1),
                                            close="expired"))
    assert "option_expiry_date" in _kinds(_hard(early))


def test_expired_on_a_long_is_a_sell(conn: sqlite3.Connection) -> None:
    _tx(conn, Side.BUY, "100", date(2026, 11, 2))
    assert _hard(validate_transaction(
        conn, _inp(Side.SELL, "100", EXPIRY, close="expired"))) == []


def test_expired_with_no_position_is_refused(conn: sqlite3.Connection) -> None:
    issues = validate_transaction(conn, _inp(Side.BUY, "100", EXPIRY, close="expired"))
    assert "option_close_no_position" in _kinds(_hard(issues))


def test_assigned_needs_a_short_and_at_most_its_size(conn: sqlite3.Connection) -> None:
    _tx(conn, Side.SELL, "300", date(2026, 11, 2), short=True)
    assert _hard(validate_transaction(
        conn, _inp(Side.BUY, "200", date(2026, 12, 15), close="assigned"))) == []
    over = validate_transaction(conn, _inp(Side.BUY, "400", date(2026, 12, 15),
                                           close="assigned"))
    assert "option_close_quantity" in _kinds(_hard(over))
    sell = validate_transaction(conn, _inp(Side.SELL, "100", date(2026, 12, 15),
                                           close="assigned"))
    assert "option_close_side_mismatch" in _kinds(_hard(sell))


def test_assigned_on_a_long_and_exercised_on_a_short_are_refused(
    conn: sqlite3.Connection,
) -> None:
    _tx(conn, Side.BUY, "100", date(2026, 11, 2), symbol=CALL)
    _tx(conn, Side.SELL, "100", date(2026, 11, 2), short=True)
    assert "option_close_direction" in _kinds(_hard(validate_transaction(
        conn, _inp(Side.BUY, "100", date(2026, 12, 15), symbol=CALL, close="assigned"))))
    assert "option_close_direction" in _kinds(_hard(validate_transaction(
        conn, _inp(Side.SELL, "100", date(2026, 12, 15), close="exercised"))))
    assert _hard(validate_transaction(
        conn, _inp(Side.SELL, "100", date(2026, 12, 15), symbol=CALL,
                   close="exercised"))) == []


def test_any_row_after_the_expiry_is_refused(conn: sqlite3.Connection) -> None:
    issues = validate_transaction(conn, _inp(Side.SELL, "100", date(2026, 12, 19),
                                             price="1", short=True))
    assert "option_after_expiry" in _kinds(_hard(issues))


def test_the_quantity_is_a_multiple_of_100_with_the_contract_hint(
    conn: sqlite3.Connection,
) -> None:
    issues = validate_transaction(conn, _inp(Side.SELL, "2", date(2026, 11, 2), price="1.5",
                                             short=True))
    hit = [i for i in issues if i.kind == "option_quantity_not_contract_multiple"]
    assert hit and not hit[0].needs_confirm
    assert hit[0].message == (
        "選擇權數量須為 100 的倍數（1 口 = 100 股當量）；若填的是口數，請改為 口數×100")


def test_a_close_reason_on_a_stock_is_refused(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="Apple"))
    _tx(conn, Side.BUY, "10", date(2026, 1, 2), symbol="AAPL", price="100")
    issues = validate_transaction(conn, _inp(Side.SELL, "10", date(2026, 2, 2),
                                             symbol="AAPL", close="expired"))
    assert "close_reason_not_option" in _kinds(_hard(issues))


def test_a_close_sell_never_raises_the_ordinary_oversell(conn: sqlite3.Connection) -> None:
    _tx(conn, Side.BUY, "100", date(2026, 11, 2))
    issues = validate_transaction(conn, _inp(Side.SELL, "200", EXPIRY, close="expired"))
    assert "sell_exceeds_holdings" not in _kinds(issues)
    assert "option_expiry_quantity" in _kinds(_hard(issues))


def test_an_unregistered_contract_in_a_tw_account_is_refused(conn: sqlite3.Connection) -> None:
    issues = validate_transaction(conn, _inp(Side.SELL, "100", date(2026, 11, 2), price="1",
                                             short=True, symbol="ABC261218P00010000",
                                             account="tw_broker"))
    assert "market_mismatch" in _kinds(_hard(issues))


# --- the CSV / edit over-cover warning ------------------------------------------------------


def test_a_buy_beyond_the_short_warns_with_the_contracts_it_opens(
    conn: sqlite3.Connection,
) -> None:
    _tx(conn, Side.SELL, "100", date(2026, 11, 2), short=True)
    issues = validate_transaction(conn, _inp(Side.BUY, "300", date(2026, 11, 20), price="1"))
    hit = [i for i in issues if i.kind == "option_over_cover"]
    assert hit and hit[0].needs_confirm
    assert hit[0].message == "買進平倉口數超過空頭部位，多出的 2 口將成為買方部位"
    exact = validate_transaction(conn, _inp(Side.BUY, "100", date(2026, 11, 20), price="1"))
    assert "option_over_cover" not in _kinds(exact)


# --- same-day after an expiry, and the stored-expired protection ----------------------------


def test_a_row_after_a_stored_expiry_on_the_same_day_is_refused(
    conn: sqlite3.Connection,
) -> None:
    _tx(conn, Side.SELL, "100", date(2026, 11, 2), short=True)
    _tx(conn, Side.BUY, "100", EXPIRY, price="0", close="expired")
    issues = validate_transaction(conn, _inp(Side.SELL, "100", EXPIRY, price="0.5",
                                             short=True))
    assert STRANDED in [i.message for i in _hard(issues)]


def test_a_back_dated_trade_cannot_strand_a_stored_expiry(conn: sqlite3.Connection) -> None:
    _tx(conn, Side.SELL, "100", date(2026, 11, 2), short=True)
    _tx(conn, Side.BUY, "100", EXPIRY, price="0", close="expired")
    issues = validate_transaction(conn, _inp(Side.SELL, "100", date(2026, 11, 10),
                                             price="1", short=True))
    assert STRANDED in [i.message for i in _hard(issues)]


def test_an_edit_that_keeps_the_expiry_whole_is_allowed(conn: sqlite3.Connection) -> None:
    sto = _tx(conn, Side.SELL, "100", date(2026, 11, 2), short=True)
    _tx(conn, Side.BUY, "100", EXPIRY, price="0", close="expired")
    stored = get_transaction(conn, sto)
    assert stored is not None
    repriced = _inp(Side.SELL, "100", date(2026, 11, 3), price="2.5", short=True)
    assert _hard(validate_transaction(conn, repriced, replacing=stored)) == []
    resized = _inp(Side.SELL, "200", date(2026, 11, 3), price="2.5", short=True)
    assert STRANDED in [i.message for i in _hard(
        validate_transaction(conn, resized, replacing=stored))]


def test_a_trade_after_the_expiry_row_on_another_contract_is_untouched(
    conn: sqlite3.Connection,
) -> None:
    _tx(conn, Side.SELL, "100", date(2026, 11, 2), short=True)
    _tx(conn, Side.BUY, "100", EXPIRY, price="0", close="expired")
    issues = validate_transaction(conn, _inp(Side.SELL, "100", date(2026, 11, 10),
                                             symbol=CALL, price="1", short=True))
    assert STRANDED not in [i.message for i in issues]


def test_the_csv_door_counts_the_siblings_booked_before(conn: sqlite3.Connection) -> None:
    """A file that opens and expires a short in one go is clean; the expiry sees the
    sibling sell above it."""
    sto = _inp(Side.SELL, "200", date(2026, 11, 2), price="1.5", short=True)
    exp = _inp(Side.BUY, "200", EXPIRY, close="expired")
    batch = [sto, exp]
    assert _hard(validate_transaction(conn, exp, batch=batch)) == []


def test_the_helper_is_callable_on_its_own(conn: sqlite3.Connection) -> None:
    """The expiry inbox confirm (W6) calls the helper directly."""
    _tx(conn, Side.SELL, "200", date(2026, 11, 2), short=True)
    inp = _inp(Side.BUY, "200", EXPIRY, close="expired")
    assert option_close_issues(conn, inp) == []
    assert option_close_issues(conn, _inp(Side.BUY, "100", EXPIRY, close="expired"))


# --- elsewhere ---------------------------------------------------------------------------------


def test_a_dividend_on_an_option_is_refused(conn: sqlite3.Connection) -> None:
    issues = validate_dividend(conn, DividendInput(
        account_id="schwab", symbol=PUT, div_date=date(2026, 11, 2), type="CASH",
        gross=Decimal("1"), withholding=Decimal("0")))
    assert "dividend_on_option" in _kinds(_hard(issues))


def _ca(kind: str, frm: str, to: str, *, d: date = date(2026, 11, 20), ratio_to: str = "1",
        ratio_from: str = "1") -> CorporateActionInput:
    return CorporateActionInput(account_id="schwab", date=d, kind=kind, from_symbol=frm,
                                to_symbol=to, ratio_to=Decimal(ratio_to),
                                ratio_from=Decimal(ratio_from))


def test_the_equity_kinds_refuse_an_option(conn: sqlite3.Connection) -> None:
    _tx(conn, Side.SELL, "100", date(2026, 11, 2), short=True)
    for kind in ("SPLIT", "EXCHANGE", "SPINOFF"):
        issues = validate_corporate_action(conn, _ca(kind, PUT, PUT if kind == "SPLIT"
                                                     else CALL))
        assert "option_in_equity_action" in _kinds(_hard(issues)), kind


def test_a_split_on_an_underlying_with_open_options_asks(conn: sqlite3.Connection) -> None:
    _tx(conn, Side.BUY, "100", date(2026, 1, 2), symbol="XYZ", price="50")
    _tx(conn, Side.SELL, "100", date(2026, 11, 2), short=True)
    issues = validate_corporate_action(conn, _ca("SPLIT", "XYZ", "XYZ", ratio_to="3"))
    hit = [i for i in issues if i.kind == "split_with_open_options"]
    assert hit and hit[0].needs_confirm
    assert hit[0].message == "此標的有未平倉選擇權，合約調整請另行登錄（選擇權合約調整）"


def test_unknown_kind_lists_the_four_kinds(conn: sqlite3.Connection) -> None:
    issues = validate_corporate_action(conn, _ca("MERGE", "XYZ", "XYZ"))
    assert issues[0].kind == "unknown_action_kind"
    assert "選擇權合約調整" in issues[0].message and "分拆" in issues[0].message


# --- OPTION_ADJUST (validation only; the replay branch is W2's) -----------------------------


ADJ_TO = "XYZ261218P00016667"   # not standard (50 × 1 ≠ 16.667 × 3)
ADJ_OK = "XYZ261218P00025000"   # 50 × 1 = 25 × 2


def test_option_adjust_standard_shape_is_clean(conn: sqlite3.Connection) -> None:
    _tx(conn, Side.SELL, "100", date(2026, 11, 2), short=True)
    issues = validate_corporate_action(
        conn, _ca("OPTION_ADJUST", PUT, ADJ_OK, ratio_to="2"))
    assert _hard(issues) == [], issues
    # The destination is not registered: it will be registered from its symbol on save.
    assert "option_contract_autoregister" in _kinds(issues)
    # Never the generic short-source / unpriced warnings — the same-direction rule replaces
    # them, and an option is never quoted.
    assert not {"short_source", "short_destination", "to_symbol_unpriced"} & _kinds(issues)


def test_option_adjust_ratio_from_must_be_one(conn: sqlite3.Connection) -> None:
    _tx(conn, Side.SELL, "100", date(2026, 11, 2), short=True)
    issues = validate_corporate_action(
        conn, _ca("OPTION_ADJUST", PUT, ADJ_OK, ratio_to="4", ratio_from="2"))
    assert "選擇權合約調整以 1 口為基準，請填 1 → N" in [i.message for i in _hard(issues)]


def test_option_adjust_nonstandard_and_shape_refusals(conn: sqlite3.Connection) -> None:
    _tx(conn, Side.SELL, "100", date(2026, 11, 2), short=True)
    bad = validate_corporate_action(conn, _ca("OPTION_ADJUST", PUT, ADJ_TO, ratio_to="3"))
    assert "option_adjust_nonstandard" in _kinds(_hard(bad))
    stock = validate_corporate_action(conn, _ca("OPTION_ADJUST", PUT, "XYZ"))
    assert "option_adjust_not_option" in _kinds(_hard(stock))
    same = validate_corporate_action(conn, _ca("OPTION_ADJUST", PUT, PUT))
    msgs = [i.message for i in _hard(same)]
    assert any("選擇權合約調整" in m for m in msgs)
    assert all("分拆" not in m and "換股" not in m for m in msgs)


def test_option_adjust_needs_a_source_position_and_one_direction(
    conn: sqlite3.Connection,
) -> None:
    none = validate_corporate_action(conn, _ca("OPTION_ADJUST", PUT, ADJ_OK, ratio_to="2"))
    hit = [i for i in _hard(none) if i.kind == "no_position_on_action_date"]
    assert hit and "選擇權合約調整" in hit[0].message
    register_option_contract(conn, ADJ_OK)
    _tx(conn, Side.SELL, "100", date(2026, 11, 2), short=True)
    _tx(conn, Side.BUY, "100", date(2026, 11, 3), symbol=ADJ_OK, price="1")
    opposite = validate_corporate_action(
        conn, _ca("OPTION_ADJUST", PUT, ADJ_OK, ratio_to="2"))
    assert "option_adjust_opposite_direction" in _kinds(_hard(opposite))
