from datetime import date
from decimal import Decimal

from portfolio_dash.pricing.board import probe_tw_board
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.shared.enums import Market


class _FakeProvider:
    def __init__(self, known: set[str]) -> None:
        self._known = known

    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        return [
            PriceRow(
                instrument=r.symbol, market=Market.TW, as_of=date(2026, 6, 10),
                close=Decimal("1"), source="fake",
            )
            for r in instruments
            if r.symbol in self._known
        ]


class _BoomProvider:
    def fetch_quote_latest(self, instruments: list[InstrumentRef]) -> list[PriceRow]:
        raise RuntimeError("network down")


def test_probe_twse() -> None:
    assert probe_tw_board("2330", twse=_FakeProvider({"2330"}), tpex=_FakeProvider(set())) == "TWSE"


def test_probe_tpex() -> None:
    assert probe_tw_board("8299", twse=_FakeProvider(set()), tpex=_FakeProvider({"8299"})) == "TPEx"


def test_probe_none_when_unknown() -> None:
    assert probe_tw_board("9999", twse=_FakeProvider(set()), tpex=_FakeProvider(set())) is None


def test_probe_graceful_on_provider_error() -> None:
    # TWSE errors -> treated as not-found -> falls through to TPEx
    assert probe_tw_board("8299", twse=_BoomProvider(), tpex=_FakeProvider({"8299"})) == "TPEx"
