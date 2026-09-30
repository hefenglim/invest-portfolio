import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from portfolio_dash.pricing.board import probe_tw_board
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.shared.enums import Market


class _FakeProvider:
    def __init__(self, known: set[str], names: dict[str, str] | None = None) -> None:
        self._known = known
        self._names = names or {}

    def fetch_quote_named(self, instrument: InstrumentRef) -> tuple[PriceRow | None, str | None]:
        if instrument.symbol not in self._known:
            return None, None
        row = PriceRow(
            instrument=instrument.symbol, market=Market.TW, as_of=date(2026, 6, 10),
            close=Decimal("1"), source="fake",
        )
        return row, self._names.get(instrument.symbol)


class _BoomProvider:
    def fetch_quote_named(self, instrument: InstrumentRef) -> tuple[PriceRow | None, str | None]:
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


# --- owner 2026-09-30, item 8: the probe hands back the exchange's own short name ----------


def test_probe_records_the_listing_boards_name() -> None:
    names: dict[str, str] = {}
    board = probe_tw_board("2330", twse=_FakeProvider({"2330"}, {"2330": "台積電"}),
                           tpex=_FakeProvider(set()), names=names)
    assert board == "TWSE" and names == {"2330": "台積電"}


def test_probe_records_the_tpex_name_when_twse_does_not_list_it() -> None:
    names: dict[str, str] = {}
    board = probe_tw_board("8299", twse=_FakeProvider(set(), {"8299": "錯的"}),
                           tpex=_FakeProvider({"8299"}, {"8299": "群聯"}), names=names)
    assert board == "TPEx" and names == {"8299": "群聯"}


def test_probe_without_a_name_leaves_the_sink_alone() -> None:
    names: dict[str, str] = {}
    assert probe_tw_board("2330", twse=_FakeProvider({"2330"}), tpex=_FakeProvider(set()),
                          names=names) == "TWSE"
    assert names == {}
    # an unlisted symbol records nothing either
    assert probe_tw_board("9999", twse=_FakeProvider(set(), {"9999": "x"}),
                          tpex=_FakeProvider(set()), names=names) is None
    assert names == {}


class _Resp:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


def test_probe_reads_the_name_from_the_recorded_exchange_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The REAL providers against recorded TWSE / TPEx payload shapes (no socket): the name
    comes out of the very response that answered the board question — one request per board,
    no extra call for the name."""
    twse_payload = json.loads(Path("tests/pricing/fixtures/twse/2330.json").read_text("utf-8"))
    tpex_rows = json.loads(Path("tests/pricing/fixtures/tpex/daily.json").read_text("utf-8"))
    calls: list[str] = []

    def _get(url: str, **kwargs: object) -> _Resp:
        calls.append(url)
        if "twse" in url:
            params = kwargs.get("params")
            assert isinstance(params, dict)
            if params["stockNo"] == "2330":
                return _Resp(twse_payload)
            return _Resp({"stat": "很抱歉，沒有符合條件的資料!"})
        return _Resp(tpex_rows)

    monkeypatch.setattr(requests, "get", _get)  # both providers call requests.get

    names: dict[str, str] = {}
    assert probe_tw_board("2330", names=names) == "TWSE"
    assert names == {"2330": "台積電"} and len(calls) == 1

    calls.clear()
    assert probe_tw_board("8299", names=names) == "TPEx"
    assert names["8299"] == "群聯" and len(calls) == 2  # TWSE miss, then the TPEx list
