"""Contract: instrument aliases (owner ruling 2026-09-30, item 8 — 「登錄名稱＋中文別名」).

A card may name an instrument by its registered name or by an alias; anything else beside its
code is a wrong pairing. So the registry now keeps an alias list per instrument, and every door
that writes one is pinned here:

* the WIRE — ``GET /api/instruments`` (and the register / update responses built from the same
  element) serve ``aliases``;
* the owner's doors — ``PUT /api/instruments/{symbol}`` and ``POST /api/instruments`` — normalize
  the list and refuse it WHOLE (422 ``validation_error``, ``field: "aliases"``, a zh sentence
  naming the alias) when an alias is a code, the instrument's own symbol, too long, or another
  instrument's code / name / alias; a refused request writes nothing at all;
* the AUTO-FILL doors — a TW registration keeps the exchange's short name the board probe read
  from its own response, and a confident AI resolve serves its common names for the dialog to
  carry into registration; there an unusable or taken name is dropped silently and the rest kept.

The exchange payloads are the RECORDED TWSE / TPEx shapes (``tests/pricing/fixtures``), served
through a patched ``requests.get`` — no socket, and no seam the production code does not use.
"""

import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import requests
from fastapi.testclient import TestClient

from portfolio_dash.api import instrument_service
from portfolio_dash.api.instrument_service import InstrumentLookup
from portfolio_dash.api.routers import instruments as inst_mod
from portfolio_dash.api.routers.instruments import AiInstrumentResolveReply
from portfolio_dash.data_ingestion.store import (
    set_instrument_aliases,
    set_instrument_archived,
    upsert_instrument,
)
from portfolio_dash.pricing.results import PriceRow, RefreshSummary
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import GOLDEN_NOW

_TWSE_FIXTURE = Path("tests/pricing/fixtures/twse/2330.json")


@pytest.fixture(autouse=True)
def _clear_ai_resolve_cache() -> Iterator[None]:
    inst_mod._AI_RESOLVE_CACHE.clear()
    yield
    inst_mod._AI_RESOLVE_CACHE.clear()


class _Resp:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


def _serve_exchange(monkeypatch: pytest.MonkeyPatch, listed: dict[str, str]) -> list[str]:
    """``requests.get`` answered from the recorded payload shapes: each code in *listed* is on
    TWSE under that short name (the STOCK_DAY title the probe reads); TPEx lists nothing."""
    base = json.loads(_TWSE_FIXTURE.read_text("utf-8"))
    calls: list[str] = []

    def _get(url: str, **kwargs: Any) -> _Resp:
        calls.append(url)
        if "twse" in url:
            code = kwargs["params"]["stockNo"]
            if code in listed:
                title = f"115年06月 {code} {listed[code]}           各日成交資訊"
                return _Resp({**base, "title": title})
            return _Resp({"stat": "很抱歉，沒有符合條件的資料!"})
        return _Resp([])

    monkeypatch.setattr(requests, "get", _get)
    return calls


def _stub_quotes(monkeypatch: pytest.MonkeyPatch, *, name: str | None = None) -> None:
    """Hermetic registration: a real price row per quote, no history, a fixed provider name,
    and no background backfill."""

    def fake_quotes(conn: Any, registry: Any, instruments: list[Any], fx_pairs: Any, *,
                    now: datetime, **_: Any) -> RefreshSummary:
        rows = [PriceRow(instrument=r.symbol, market=r.market, as_of=now.date(),
                         close=Decimal("100"), source="stub") for r in instruments]
        upsert_prices(conn, rows, fetched_at=now)
        return RefreshSummary(ok={r.symbol: "stub" for r in instruments}, failed=[],
                              fetched_at=now)

    def fake_history(conn: Any, registry: Any, instruments: list[Any], start: date, *,
                     now: datetime, **_: Any) -> RefreshSummary:
        return RefreshSummary(ok={}, failed=[], fetched_at=now)

    monkeypatch.setattr(instrument_service, "refresh_quotes", fake_quotes)
    monkeypatch.setattr(instrument_service, "refresh_history", fake_history)
    monkeypatch.setattr(instrument_service, "lookup_name",
                        lambda sym, market, *, board=None: name)
    monkeypatch.setattr(inst_mod, "gap_backfill", lambda *a, **k: None)


def _row(api_client: TestClient, symbol: str) -> dict[str, Any]:
    rows: dict[str, dict[str, Any]] = {
        i["symbol"]: i for i in api_client.get("/api/instruments").json()["list"]}
    return rows[symbol]


def _refused(r: Any) -> str:
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_error" and err["field"] == "aliases"
    message: str = err["message"]
    return message


# --- the wire ------------------------------------------------------------------------------


def test_get_serves_an_alias_list_on_every_row(api_client: TestClient) -> None:
    rows = api_client.get("/api/instruments").json()["list"]
    assert rows and all(r["aliases"] == [] for r in rows)


# --- the edit door: store / leave / clear ---------------------------------------------------


def test_update_stores_the_normalized_list(api_client: TestClient) -> None:
    r = api_client.put("/api/instruments/2330", json={
        "aliases": [" 台積電 ", "台積電", "tsmc", "Taiwan  Semi", ""]})
    assert r.status_code == 200, r.text
    # trimmed, inner spaces collapsed, the duplicate and the registered name (TSMC) dropped
    assert r.json()["aliases"] == ["台積電", "Taiwan Semi"]
    assert _row(api_client, "2330")["aliases"] == ["台積電", "Taiwan Semi"]


def test_update_without_aliases_leaves_them_and_an_empty_list_clears(
    api_client: TestClient,
) -> None:
    assert api_client.put("/api/instruments/2330",
                          json={"aliases": ["台積電"]}).status_code == 200
    assert api_client.put("/api/instruments/2330",
                          json={"sector": "Information Technology"}).status_code == 200
    assert api_client.put("/api/instruments/2330", json={"aliases": None}).status_code == 200
    assert _row(api_client, "2330")["aliases"] == ["台積電"]
    r = api_client.put("/api/instruments/2330", json={"aliases": []})
    assert r.status_code == 200 and r.json()["aliases"] == []
    assert _row(api_client, "2330")["aliases"] == []


def test_update_checks_aliases_against_the_new_name(api_client: TestClient) -> None:
    """Renaming 2330 to 台積電 in the same save drops the now-identical alias."""
    r = api_client.put("/api/instruments/2330",
                       json={"name": "台積電", "aliases": ["台積電", "TSMC"]})
    assert r.status_code == 200
    assert r.json()["name"] == "台積電" and r.json()["aliases"] == ["TSMC"]


# --- the edit door: refusals ----------------------------------------------------------------


def test_update_refuses_a_code_and_writes_nothing(api_client: TestClient) -> None:
    msg = _refused(api_client.put("/api/instruments/2330",
                                  json={"name": "改名", "aliases": ["台積電", "1234"]}))
    assert "別名「1234」是數字" in msg
    row = _row(api_client, "2330")
    assert row["name"] == "TSMC" and row["aliases"] == []  # the rename did not land either


def test_update_refuses_the_instruments_own_symbol(api_client: TestClient) -> None:
    msg = _refused(api_client.put("/api/instruments/AAPL", json={"aliases": ["aapl"]}))
    assert msg == "別名「aapl」就是這檔標的的代號"


def test_update_refuses_an_alias_that_is_too_long(api_client: TestClient) -> None:
    msg = _refused(api_client.put("/api/instruments/AAPL", json={"aliases": ["蘋" * 31]}))
    assert "超過 30 個字" in msg


def test_update_refuses_another_instruments_name_symbol_or_alias(
    api_client: TestClient,
) -> None:
    # another instrument's registered NAME (case-blind)
    msg = _refused(api_client.put("/api/instruments/AAPL", json={"aliases": ["tsmc"]}))
    assert msg == "別名「tsmc」已屬於 2330（TSMC）"
    # another instrument's SYMBOL
    msg = _refused(api_client.put("/api/instruments/2330", json={"aliases": ["AAPL"]}))
    assert msg == "別名「AAPL」已屬於 AAPL（Apple）"
    # another instrument's ALIAS — and every conflict is named at once
    assert api_client.put("/api/instruments/2330",
                          json={"aliases": ["台積電"]}).status_code == 200
    msg = _refused(api_client.put("/api/instruments/AAPL",
                                  json={"aliases": ["蘋果", "台積電", "TSMC"]}))
    assert msg == "別名「台積電」已屬於 2330（TSMC）；別名「TSMC」已屬於 2330（TSMC）"
    assert _row(api_client, "AAPL")["aliases"] == []


# --- the register door ------------------------------------------------------------------------


def test_register_stores_the_dialogs_aliases(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_quotes(monkeypatch)
    r = api_client.post("/api/instruments", json={
        "symbol": "MSFT", "market": "US", "name": "Microsoft", "aliases": ["微軟", "微軟"]})
    assert r.status_code == 201, r.text
    assert r.json()["aliases"] == ["微軟"]
    assert _row(api_client, "MSFT")["aliases"] == ["微軟"]


def test_register_refuses_a_taken_alias_and_registers_nothing(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_quotes(monkeypatch)
    msg = _refused(api_client.post("/api/instruments", json={
        "symbol": "MSFT", "market": "US", "name": "Microsoft", "aliases": ["Apple"]}))
    assert msg == "別名「Apple」已屬於 AAPL（Apple）"
    symbols = {i["symbol"] for i in api_client.get("/api/instruments").json()["list"]}
    assert "MSFT" not in symbols


def _name_refused(r: Any) -> str:
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_error" and err["field"] == "name"
    message: str = err["message"]
    return message


def test_a_typed_name_that_is_another_instruments_alias_is_refused(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror of the alias rule: renaming AAPL to 「台積電」 while 2330 answers to it would
    make both 「台積電 (AAPL)」 and 「台積電 (2330)」 right. Refused at both typed-name doors,
    before anything is written; another instrument's registered NAME stays allowed."""
    _stub_quotes(monkeypatch)
    assert api_client.put("/api/instruments/2330",
                          json={"aliases": ["台積電"]}).status_code == 200
    msg = _name_refused(api_client.put("/api/instruments/AAPL", json={"name": "台積電"}))
    assert msg == "名稱「台積電」已是 2330（TSMC）的別名"
    assert _row(api_client, "AAPL")["name"] == "Apple"
    msg = _name_refused(api_client.post("/api/instruments", json={
        "symbol": "MSFT", "market": "US", "name": "台積電"}))
    assert msg == "名稱「台積電」已是 2330（TSMC）的別名"
    symbols = {i["symbol"] for i in api_client.get("/api/instruments").json()["list"]}
    assert "MSFT" not in symbols
    # another instrument's registered NAME is no conflict (GOOG / GOOGL share one)
    assert api_client.put("/api/instruments/AAPL", json={"name": "TSMC"}).status_code == 200
    # nor is its own alias as its own name — and the alias then leaves the list
    r = api_client.put("/api/instruments/2330", json={"name": "台積電"})
    assert r.status_code == 200 and r.json()["name"] == "台積電" and r.json()["aliases"] == []


def test_register_restoring_an_archived_symbol_replaces_aliases_only_when_given(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_quotes(monkeypatch)
    assert api_client.post("/api/instruments", json={
        "symbol": "MSFT", "market": "US", "name": "Microsoft",
        "aliases": ["微軟"]}).status_code == 201
    assert api_client.delete("/api/instruments/MSFT").status_code == 200  # soft delete
    # re-added with a blank field: the archived row keeps its list
    r = api_client.post("/api/instruments", json={"symbol": "MSFT", "market": "US"})
    assert r.status_code == 201 and r.json()["restored"] is True
    assert r.json()["aliases"] == ["微軟"]
    assert api_client.delete("/api/instruments/MSFT").status_code == 200
    # re-added with a list: it replaces
    r = api_client.post("/api/instruments", json={
        "symbol": "MSFT", "market": "US", "aliases": ["微軟公司", "Microsoft"]})
    assert r.status_code == 201 and r.json()["restored"] is True
    assert r.json()["aliases"] == ["微軟公司"]  # the one equal to the name dropped


def test_quick_register_restore_honours_the_doors_aliases(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The service's own restore branch (FU-D18 door c): a door that hands ``quick_register``
    a checked list for an ARCHIVED symbol gets it stored, and a blank list keeps the old one."""
    _stub_quotes(monkeypatch)
    monkeypatch.setattr(instrument_service, "gap_backfill", lambda *a, **k: None)
    upsert_instrument(golden_db, Instrument(symbol="MSFT", market=Market.US,
                                            quote_ccy=Currency.USD, sector="", name="Microsoft"))
    set_instrument_aliases(golden_db, "MSFT", ["微軟"])
    set_instrument_archived(golden_db, "MSFT", True)
    out = instrument_service.quick_register(golden_db, symbol="MSFT", market=Market.US,
                                            now=GOLDEN_NOW)
    assert out.restored and out.instrument.aliases == ["微軟"]
    set_instrument_archived(golden_db, "MSFT", True)
    out = instrument_service.quick_register(golden_db, symbol="MSFT", market=Market.US,
                                            now=GOLDEN_NOW, aliases=["軟體巨頭"])
    assert out.restored and out.instrument.aliases == ["軟體巨頭"]


def test_register_tw_keeps_the_exchange_short_name_from_the_probe(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No board from the dialog → the service probes, and the probe's own TWSE response
    (「… 3008 大立光 各日成交資訊」) names the symbol — kept as an alias beside LARGAN."""
    _stub_quotes(monkeypatch)
    calls = _serve_exchange(monkeypatch, {"3008": "大立光"})
    r = api_client.post("/api/instruments",
                        json={"symbol": "3008", "market": "TW", "name": "LARGAN"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["board"] == "TWSE" and body["aliases"] == ["大立光"]
    assert len(calls) == 1  # the board probe's one request — nothing extra for the name


def test_register_tw_with_no_name_found_takes_the_exchange_short_name(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No typed name and the name lookup finds none → the exchange's short name the probe
    already read becomes the NAME (not a blank row), and so is not repeated as an alias. The
    lookup offers the same name before anything is written."""
    _stub_quotes(monkeypatch, name=None)
    _serve_exchange(monkeypatch, {"3008": "大立光"})
    looked = api_client.get("/api/instruments/lookup",
                            params={"symbol": "3008", "market": "TW"}).json()
    assert looked["name"] == "大立光" and looked["aliases"] == []
    r = api_client.post("/api/instruments", json={"symbol": "3008", "market": "TW"})
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "大立光" and r.json()["aliases"] == []


def test_register_settles_aliases_against_the_name_the_row_ends_with(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The door checks against the name it was given (blank here); the provider fills the name
    AFTER that — so the stored list is settled against the final name, and the dialog's copy
    of the exchange name and the probe's own copy count once."""
    _stub_quotes(monkeypatch, name="LARGAN PRECISION CO LTD")
    _serve_exchange(monkeypatch, {"3008": "大立光"})
    r = api_client.post("/api/instruments", json={
        "symbol": "3008", "market": "TW",
        "aliases": ["大立光", "largan precision co ltd"]})
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "LARGAN PRECISION CO LTD"
    assert r.json()["aliases"] == ["大立光"]


def test_register_tw_exchange_name_equal_to_the_name_adds_nothing(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_quotes(monkeypatch, name="大立光")  # the provider's name IS the exchange's
    _serve_exchange(monkeypatch, {"3008": "大立光"})
    r = api_client.post("/api/instruments", json={"symbol": "3008", "market": "TW"})
    assert r.status_code == 201 and r.json()["name"] == "大立光"
    assert r.json()["aliases"] == []


def test_register_tw_drops_a_taken_exchange_name_silently(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An auto-fill never fails a registration: the exchange name another instrument already
    answers to is dropped, the owner's own alias kept, the row registered."""
    assert api_client.put("/api/instruments/2330",
                          json={"aliases": ["大立光"]}).status_code == 200
    _stub_quotes(monkeypatch)
    _serve_exchange(monkeypatch, {"3008": "大立光"})
    r = api_client.post("/api/instruments", json={
        "symbol": "3008", "market": "TW", "name": "LARGAN", "aliases": ["Largan Precision"]})
    assert r.status_code == 201, r.text
    assert r.json()["aliases"] == ["Largan Precision"]


def test_quick_add_door_keeps_the_exchange_short_name(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_quotes(monkeypatch, name="LARGAN PRECISION CO LTD")
    _serve_exchange(monkeypatch, {"3008": "大立光"})
    r = api_client.post("/api/instruments/quick", json={"symbol": "3008", "market": "TW"})
    assert r.status_code == 201, r.text
    assert r.json()["aliases"] == ["大立光"]


def test_manual_trade_auto_register_door_keeps_the_exchange_short_name(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third door into ``quick_register``: a manual trade on an unregistered TW code."""
    _stub_quotes(monkeypatch, name="LARGAN PRECISION CO LTD")
    _serve_exchange(monkeypatch, {"3008": "大立光"})
    r = api_client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "3008", "side": "buy",
        "date": "2026-06-11", "shares": "100", "price": "10"})
    assert r.status_code == 201, r.text
    assert r.json()["auto_registered"]["symbol"] == "3008"
    assert _row(api_client, "3008")["aliases"] == ["大立光"]


# --- the lookup (the dialog's first step; a GET — it offers, never writes) ------------------


def test_lookup_offers_the_exchange_short_name_without_writing(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_quotes(monkeypatch, name="LARGAN PRECISION CO LTD")
    _serve_exchange(monkeypatch, {"3008": "大立光"})
    r = api_client.get("/api/instruments/lookup", params={"symbol": "3008", "market": "TW"})
    body = r.json()
    assert body["found"] is True and body["board"] == "TWSE"
    assert body["aliases"] == ["大立光"]
    symbols = {i["symbol"] for i in api_client.get("/api/instruments").json()["list"]}
    assert "3008" not in symbols


def test_lookup_of_a_known_symbol_serves_its_stored_aliases(api_client: TestClient) -> None:
    assert api_client.put("/api/instruments/2330",
                          json={"aliases": ["台積電"]}).status_code == 200
    r = api_client.get("/api/instruments/lookup", params={"symbol": "2330", "market": "TW"})
    assert r.json()["registered"] is True and r.json()["aliases"] == ["台積電"]


# --- AI resolve ---------------------------------------------------------------------------------


def _completer(reply: AiInstrumentResolveReply) -> Callable[..., AiInstrumentResolveReply]:
    def _f(*_a: object, **_k: object) -> AiInstrumentResolveReply:
        return reply
    return _f


def _lookup_found(name: str) -> Callable[..., InstrumentLookup]:
    def _f(*_a: object, **_k: object) -> InstrumentLookup:
        return InstrumentLookup(found=True, registered=False, name=name, sector="", board="")
    return _f


def test_ai_resolve_serves_its_common_names_filtered(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolved reply carries the model's common names, minus the unusable (its own code, a
    number, the name itself, a repeat) and the taken (Apple is AAPL's) — silently; an
    uncertain one carries none."""
    monkeypatch.setattr(inst_mod, "complete_structured", _completer(AiInstrumentResolveReply(
        symbol="MSFT", name="Microsoft", gics_sector="Information Technology",
        confidence="high",
        aliases=["微軟", "MSFT", "1234", "Microsoft Corporation", "Apple", "微軟"])))
    monkeypatch.setattr(inst_mod, "lookup_instrument", _lookup_found("Microsoft Corporation"))
    r = api_client.post("/api/instruments/ai-resolve", json={"query": "微軟", "market": "US"})
    body = r.json()
    assert body["status"] == "resolved" and body["name"] == "Microsoft Corporation"
    assert body["aliases"] == ["微軟"]

    # An UNCERTAIN reply (candidates) teaches the registry no names: the model was not sure
    # which instrument it meant, so its names for it are not served at all.
    inst_mod._AI_RESOLVE_CACHE.clear()
    monkeypatch.setattr(inst_mod, "complete_structured", _completer(AiInstrumentResolveReply(
        symbol="MSFT", name="Microsoft", gics_sector="Information Technology",
        confidence="medium", aliases=["微軟"])))
    body = api_client.post("/api/instruments/ai-resolve",
                           json={"query": "微軟", "market": "US"}).json()
    assert body["status"] == "candidates"
    assert "aliases" not in body and all("aliases" not in c for c in body["candidates"])


def test_ai_resolve_keeps_a_chinese_model_name_the_exchange_name_replaced(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEF-088 (verifier R13, F-01): for a TW name the provider's name is the exchange short
    name (長榮航); the model, following the prompt's example, puts the full name in ``name``
    and the short one in ``aliases``. Replacing the name dropped 長榮航空, and 長榮航 then
    equalled the name — every TW reply served no alias (長榮航, 華航, 聯電 alike)."""
    monkeypatch.setattr(inst_mod, "complete_structured", _completer(AiInstrumentResolveReply(
        symbol="2618", name="長榮航空", gics_sector="Industrials", confidence="high",
        aliases=["長榮航"])))
    monkeypatch.setattr(inst_mod, "lookup_instrument", _lookup_found("長榮航"))
    body = api_client.post("/api/instruments/ai-resolve",
                           json={"query": "長榮航", "market": "TW"}).json()
    assert body["status"] == "resolved" and body["name"] == "長榮航"
    assert body["aliases"] == ["長榮航空"]


def test_ai_resolve_registered_short_circuit_serves_stored_aliases(
    api_client: TestClient,
) -> None:
    assert api_client.put("/api/instruments/2330",
                          json={"aliases": ["台積電"]}).status_code == 200
    body = api_client.post("/api/instruments/ai-resolve",
                           json={"query": "2330", "market": "TW"}).json()
    assert body["status"] == "resolved" and body["aliases"] == ["台積電"]


def test_a_registration_through_ai_resolve_stores_its_names(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dialog's path, end to end on the API: resolve → carry ``aliases`` → register."""
    monkeypatch.setattr(inst_mod, "complete_structured", _completer(AiInstrumentResolveReply(
        symbol="MSFT", name="Microsoft", gics_sector="Information Technology",
        confidence="high", aliases=["微軟", "Apple"])))
    monkeypatch.setattr(inst_mod, "lookup_instrument", _lookup_found("Microsoft"))
    resolved = api_client.post("/api/instruments/ai-resolve",
                               json={"query": "微軟", "market": "US"}).json()
    _stub_quotes(monkeypatch)
    r = api_client.post("/api/instruments", json={
        "symbol": resolved["symbol"], "market": "US", "name": resolved["name"],
        "aliases": resolved["aliases"]})
    assert r.status_code == 201, r.text
    assert _row(api_client, "MSFT")["aliases"] == ["微軟"]


def test_the_audit_reader_prints_a_stored_alias_list_as_names() -> None:
    """A purged instrument's audit snapshot carries its aliases (``SELECT *``): the reader
    labels the column and prints the names joined, never the stored JSON."""
    from portfolio_dash.export.ledger_audit import FIELD_LABELS, _display

    assert FIELD_LABELS["aliases"] == "別名"
    assert _display("instruments", "aliases", '["大立光", "LARGAN"]') == "大立光、LARGAN"
    assert _display("instruments", "aliases", "[]") == "無"
