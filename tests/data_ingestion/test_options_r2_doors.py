"""Options final review R2 — the data_ingestion half (spec §4.2, §8).

* M1 — the validator inputs canonicalise an OCC symbol the moment they are built, so every
  door that builds one (the transactions CSV, the AI door, the corporate-action CSV) validates
  and writes ONE spelling; a stock symbol is untouched.
* m3 — 「TRANSFER OF SECURITY OR OPTION IN」 is an option-only rule: a STOCK in-transfer stops
  the run with ``UnmappedRow`` exactly as on ``main``.
* m4 — same-day adjustment journals of two underlyings are two pairs, not one group of four.

(nit (d) — a close row never reaches the sell guard, so the expiry inbox's 賣超 branch was
unreachable and is removed — is proven by ``test_options_w3_validate.py::
test_a_close_sell_never_raises_the_ordinary_oversell``.)
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.broker import schwab
from portfolio_dash.data_ingestion.broker.convert import convert
from portfolio_dash.data_ingestion.broker.ir import EventKind, RawEvent, UnmappedRow
from portfolio_dash.data_ingestion.validate import CorporateActionInput, TxnInput
from portfolio_dash.shared.models.enums import Side

_HEADER = '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
PUT = "XYZ261218P00050000"
C60 = "XYZ261218C00060000"
C20 = "XYZ261218C00020000"
A30 = "ABC261218C00030000"
A15 = "ABC261218C00015000"


def _parse(*rows: str) -> list[RawEvent]:
    return schwab.parse(_HEADER + "".join(r + "\n" for r in rows), source_file="s.csv")


# --- M1: one spelling from the moment an input exists ----------------------------------------


@pytest.mark.parametrize("typed", [PUT.lower(), f"  {PUT.lower()} ", "xyz261218P00050000"])
def test_a_transaction_input_spells_a_contract_canonically(typed: str) -> None:
    inp = TxnInput(account_id="schwab", symbol=typed, side=Side.BUY, quantity=Decimal("100"),
                   price=Decimal("1"), trade_date=date(2026, 6, 1))
    assert inp.symbol == PUT


@pytest.mark.parametrize("symbol", ["AAPL", "aapl", "2330", "BRK.B", " AAPL "])
def test_a_stock_symbol_is_left_exactly_as_typed(symbol: str) -> None:
    """Only the OCC shape is canonicalised — a stock symbol's handling is unchanged."""
    inp = TxnInput(account_id="schwab", symbol=symbol, side=Side.BUY, quantity=Decimal("1"),
                   price=Decimal("1"), trade_date=date(2026, 6, 1))
    assert inp.symbol == symbol


def test_a_corporate_action_input_spells_both_contracts_canonically() -> None:
    inp = CorporateActionInput(account_id="schwab", date=date(2026, 5, 10),
                               kind="OPTION_ADJUST", from_symbol=C60.lower(),
                               to_symbol=f" {C20.lower()}", ratio_to=Decimal(3),
                               ratio_from=Decimal(1))
    assert (inp.from_symbol, inp.to_symbol) == (C60, C20)
    stock = CorporateActionInput(account_id="schwab", date=date(2026, 5, 10), kind="SPLIT",
                                 from_symbol="aapl", to_symbol="aapl", ratio_to=Decimal(2),
                                 ratio_from=Decimal(1))
    assert (stock.from_symbol, stock.to_symbol) == ("aapl", "aapl")


# --- m3: the in-transfer rule is for option contracts only -----------------------------------


def test_a_stock_in_transfer_stops_the_run_as_on_main() -> None:
    with pytest.raises(UnmappedRow) as exc:
        _parse('09/20/2025,Journaled Shares,AAA,'
               '"TDA TRAN - TRANSFER OF SECURITY OR OPTION IN (AAA)",10,,,$0.00')
    assert exc.value.line_no == 2
    assert exc.value.reason is None


def test_an_option_in_transfer_is_still_recognised() -> None:
    (event,) = _parse('09/20/2026,Journaled Shares,XYZ 12/18/2026 50.00 P,'
                      '"TDA TRAN - TRANSFER OF SECURITY OR OPTION IN (XYZ)",1,,,$0.00')
    assert event.kind is EventKind.TRANSFER_OUT


# --- m4: adjustment pairs are grouped per underlying ------------------------------------------


def test_same_day_adjustments_of_two_underlyings_are_two_pairs() -> None:
    events = _parse(
        '11/03/2026,Buy to Open,XYZ 12/18/2026 60.00 C,"BUY TO OPEN",2,$2.00,$1.30,-$401.30',
        '11/03/2026,Buy to Open,ABC 12/18/2026 30.00 C,"BUY TO OPEN",1,$1.00,$0.65,-$100.65',
        '11/10/2026,Journaled Shares,XYZ 12/18/2026 60.00 C,'
        '"TDA TRAN - OPTION POSITION CHANGE (XYZ)",-2,,,$0.00',
        '11/10/2026,Journaled Shares,XYZ 12/18/2026 20.00 C,'
        '"TDA TRAN - OPTION POSITION CHANGE (XYZ)",6,,,$0.00',
        '11/10/2026,Journaled Shares,ABC 12/18/2026 30.00 C,'
        '"TDA TRAN - OPTION POSITION CHANGE (ABC)",-1,,,$0.00',
        '11/10/2026,Journaled Shares,ABC 12/18/2026 15.00 C,'
        '"TDA TRAN - OPTION POSITION CHANGE (ABC)",2,,,$0.00',
    )
    conv, _g, _r = convert(events, "schwab", "USD", known_positions={})
    actions = sorted((r[3], r[4], r[5]) for r in conv.rows["corporate_actions"])
    assert actions == [(A30, A15, "2"), (C60, C20, "3")]
