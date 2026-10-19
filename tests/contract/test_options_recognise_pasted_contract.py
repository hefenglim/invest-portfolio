"""Owner ruling 2026-10-10 — the manual page recognises a pasted contract code by itself.

``GET /api/input/option-symbol?text=…`` (options spec §8, the compose endpoint) answers whether a
string typed or pasted into the manual form's 代號 box is an option contract: an OCC symbol in
any case, padded or not, or a Schwab statement string (``XYZ 12/18/2026 50.00 P``,
``shared/options.py::from_broker_option``) → ``200 {"symbol": <canonical OCC>, "name": …}``;
anything else → ``200 {"symbol": null}``. NEVER a 4xx for a non-contract: the page asks this
for every ticker its own list does not know, and a 4xx is a console error that every
zero-console-error e2e treats as a failure. The browser never parses the shape itself.

Read-only (DEF-065): recognising a contract registers nothing — the commit that uses it does.
The ``underlying&expiry&strike&right`` form is unchanged.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import get_instrument, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument

PUT = "XYZ261218P00050000"
URL = "/api/input/option-symbol"
_CONTRACT = {"symbol": PUT, "name": "XYZ 2026-12-18 50 Put", "underlying": "XYZ",
             "right": "P", "expiry": "2026-12-18", "strike": "50"}


@pytest.mark.parametrize("text", [
    "xyz261218p00050000",            # lower case
    "XYZ261218P00050000",
    "  XYZ261218P00050000  ",        # surrounding space
    "XYZ   261218P00050000",         # the 21-character OCC form, root padded to six
    "XYZ 12/18/2026 50.00 P",        # a Schwab statement string
    "xyz 12/18/2026 50 p",
])
def test_a_contract_code_is_recognised(api_client: TestClient, text: str) -> None:
    r = api_client.get(URL, params={"text": text})
    assert r.status_code == 200, r.text
    assert r.json() == _CONTRACT


@pytest.mark.parametrize("text", [
    "AAPL", "2330", "1155", "BRK.B",                 # ordinary tickers, known or not
    "", "   ", "zz!!garbage", "XYZ261318P00050000",  # month 13: the shape is not enough
    "XYZ261218P00000000",                            # a zero strike is not a contract
    "XYZ1261218P00050000",                           # a digit root (an adjusted contract)
    "XYZ 12/18/2026 50.00 X",
])
def test_anything_else_answers_null_never_a_4xx(api_client: TestClient, text: str) -> None:
    r = api_client.get(URL, params={"text": text})
    assert r.status_code == 200, r.text
    assert r.json() == {"symbol": None}


def test_a_root_registered_in_another_market_is_not_a_contract(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """The compose form refuses it (options are US only, R6); recognition does not offer it."""
    upsert_instrument(golden_db, Instrument(symbol="XYZ", market=Market.TW,
                                            quote_ccy=Currency.TWD, sector="", name="XYZ"))
    r = api_client.get(URL, params={"text": PUT})
    assert r.status_code == 200, r.text
    assert r.json() == {"symbol": None}


def test_recognising_registers_nothing(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    before = golden_db.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
    for text in ("xyz261218p00050000", "XYZ 12/18/2026 50.00 P"):
        assert api_client.get(URL, params={"text": text}).json()["symbol"] == PUT
    assert get_instrument(golden_db, PUT) is None
    assert golden_db.execute("SELECT COUNT(*) FROM instruments").fetchone()[0] == before


def test_the_compose_form_is_unchanged(api_client: TestClient) -> None:
    r = api_client.get(URL, params={"underlying": "xyz", "expiry": "2026-12-18",
                                    "strike": "50", "right": "P"})
    assert r.status_code == 200, r.text
    assert r.json() == {"symbol": PUT, "name": "XYZ 2026-12-18 50 Put"}
    r = api_client.get(URL, params={"underlying": "XYZ", "expiry": "12/18/2026",
                                    "strike": "50", "right": "P"})
    assert r.status_code == 400 and "到期日" in r.json()["error"]["message"]
