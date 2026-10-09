"""F-3 — the never-500 rule needs a NET, not one guard per call site.

`portfolio/dashboard.py` re-types a decimal fault from ``build_book`` into an
``UnbookableLedgerError`` carrying a sentence the owner can act on — and then nothing catches
it: ``api/routers/dashboard.py`` calls ``build_dashboard`` bare, so the sentence was swallowed
by ``api/errors.py``'s catch-all and ``GET /api/dashboard`` answered **500 「系統發生未預期的
錯誤」**. The nine other ``build_book(..., allow_oversell=True)`` call sites carry the same
exposure, and ``OversellError`` is a SEPARATE hierarchy (``Exception``, not ``ValueError``)
that has already had to be fixed one router at a time (``strategy/whatif.py`` 2026-08-11,
``api/routers/export.py`` QA-06). Two handlers close the class instead of the instances.

The guard being re-typed was also too narrow. ``decimal.InvalidOperation`` is one leaf of
``ArithmeticError``; ``decimal.Overflow`` and ``decimal.DivisionByZero`` are siblings, NOT
subclasses, so they walked straight past it. Measured on this exact fixture, before the fix:
a single direct-SQL row with ``quantity='1E+999999'`` raises ``decimal.Overflow``
(``isinstance(exc, ArithmeticError)`` True, ``isinstance(exc, InvalidOperation)`` **False**)
out of ``cost_basis.py:508`` and ``GET /api/dashboard`` answered 500. ``strategy/whatif.py``
already says of its own arm that it "is for the class, not the cause"; this makes that true.

Routers that catch these locally keep precedence and are deliberately untouched — the handlers
are the floor, not a replacement for a seam that can say something more specific.
"""

import re
import sqlite3
from collections.abc import Callable, Iterator
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api.errors import register_error_handlers
from portfolio_dash.portfolio.cost_basis import OversellError, UnbookableLedgerError

_ASCII_ONLY = re.compile(r"^[\x00-\x7f]*$")
_CJK = re.compile(r"[一-鿿]")


@pytest.fixture
def raising_client() -> Iterator[Callable[[Exception], TestClient]]:
    """A one-route app wired to the REAL handlers, whose route raises *exc*.

    Same shape (and the same socket note) as ``tests/api/test_r5_error_envelope_zh.py``:
    sockets are re-enabled because the TestClient's anyio portal uses a real self-pipe on
    Windows. Nothing leaves the process.
    """
    enable_socket()

    def _make(exc: Exception) -> TestClient:
        app = FastAPI()
        register_error_handlers(app)

        @app.get("/boom")
        def _boom() -> None:
            raise exc

        return TestClient(app, raise_server_exceptions=False)

    try:
        yield _make
    finally:
        disable_socket(allow_unix_socket=True)


def _error(client: TestClient, status: int) -> dict[str, object]:
    resp = client.get("/boom")
    assert resp.status_code == status, f"{resp.status_code}: {resp.text[:300]}"
    body = resp.json()["error"]
    assert isinstance(body, dict)
    return body


# --- UnbookableLedgerError ------------------------------------------------------------

def test_an_unbookable_ledger_answers_422_with_the_replays_own_sentence(
    raising_client: Callable[[Exception], TestClient],
) -> None:
    """★ The exact text ``portfolio/dashboard.py`` re-types the decimal fault into."""
    message = "帳本中有一列的數值無法計算（例如股數為 0 的交易），請於交易帳本修正該列後重試"
    err = _error(raising_client(UnbookableLedgerError(message)), 422)
    assert err["code"] == "unbookable_ledger"
    assert err["message"] == message


def test_the_unbookable_message_is_never_english(
    raising_client: Callable[[Exception], TestClient],
) -> None:
    """Every raise site speaks zh today; ``_prefer_zh`` keeps that true if one stops."""
    err = _error(raising_client(UnbookableLedgerError("cannot book row 7")), 422)
    message = str(err["message"])
    assert not _ASCII_ONLY.match(message), message
    assert _CJK.search(message), message


def test_a_plain_value_error_is_still_an_internal_error(
    raising_client: Callable[[Exception], TestClient],
) -> None:
    """``UnbookableLedgerError`` subclasses ``ValueError`` — the handler must not widen to it.

    A bare ``ValueError`` from anywhere in the app is a programming fault, and answering 422
    「請修正帳本」 for it would send the owner to edit a ledger that is not the problem.
    """
    assert _error(raising_client(ValueError("這不是帳本問題")), 500)["code"] == "internal_error"


# --- OversellError --------------------------------------------------------------------

def _oversell() -> OversellError:
    return OversellError("sell 100 > held 50 for 2330", account_id="tw_broker",
                         symbol="2330", trade_date=date(2026, 2, 1))


def test_an_oversell_answers_422_naming_the_offending_row(
    raising_client: Callable[[Exception], TestClient],
) -> None:
    """★ ``OversellError`` is not a ``ValueError``, so the ``UnbookableLedgerError`` arm above
    can never catch it — the mistake ``whatif.py`` and ``export.py`` each had to fix alone."""
    err = _error(raising_client(_oversell()), 422)
    assert err["code"] == "oversold_position"
    message = str(err["message"])
    assert _CJK.search(message), message
    assert "tw_broker" in message and "2330" in message and "2026-02-01" in message


def test_the_english_exception_text_stays_out_of_the_message(
    raising_client: Callable[[Exception], TestClient],
) -> None:
    """``str(exc)`` is ``sell 100 > held 50 for 2330`` — rendered verbatim as a red toast."""
    message = str(_error(raising_client(_oversell()), 422)["message"])
    assert not _ASCII_ONLY.match(message), message
    assert "held" not in message, message


def test_the_oversell_issue_carries_the_row_as_FIELDS_not_prose(
    raising_client: Callable[[Exception], TestClient],
) -> None:
    """The same ``issues`` shape ``export.py`` and ``whatif.py`` emit, so one frontend
    branch renders all three: a caller must never have to regex a sentence for the row."""
    issues = _error(raising_client(_oversell()), 422)["issues"]
    assert isinstance(issues, list)
    (issue,) = issues
    assert issue == {
        "sev": "error",
        "code": "oversold_position",
        "text": "sell 100 > held 50 for 2330",
        "field": None,
        "account_id": "tw_broker",
        "symbol": "2330",
        "trade_date": "2026-02-01",
    }


# --- the catch-all is still the catch-all ---------------------------------------------

def test_an_unrelated_error_still_answers_500(
    raising_client: Callable[[Exception], TestClient],
) -> None:
    """Control: the new handlers narrow nothing. A real fault is still a real fault."""
    err = _error(raising_client(RuntimeError("boom")), 500)
    assert err["code"] == "internal_error"
    assert "boom" not in str(err["message"])       # never leak the detail


# --- end to end, on a real ledger -----------------------------------------------------
#
# A row whose quantity x price OVERFLOWS the Decimal context. Reachable only by editing the
# database (every write door validates), so it is inserted by direct SQL exactly as
# `tests/contract/test_m2_negative_quantity_never_500.py` inserts its negative quantity.
# Measured before the fix: `decimal.Overflow` out of `cost_basis.py:508`, ArithmeticError but
# NOT InvalidOperation, straight through `build_dashboard`'s guard -> HTTP 500.

def _plant_overflow_row(conn: sqlite3.Connection, side: str = "BUY") -> None:
    conn.execute(
        "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax,"
        " trade_date) VALUES ('tw_broker','2330',?,'1E+999999','600','0','0',"
        "'2026-02-01')", (side,))
    conn.commit()


def test_the_dashboard_answers_422_not_500_over_an_uncomputable_row(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """★ Both halves at once: the widened guard re-types the ``Overflow``, and the new
    handler turns that into an envelope instead of 「系統發生未預期的錯誤」."""
    _plant_overflow_row(golden_db)
    client = TestClient(api_client.app, raise_server_exceptions=False)
    resp = client.get("/api/dashboard")
    assert resp.status_code == 422, f"HTTP {resp.status_code}: {resp.text[:300]}"
    err = resp.json()["error"]
    assert err["code"] == "unbookable_ledger"
    assert _CJK.search(str(err["message"])), err["message"]


# --- R-1: the same row on the OTHER side, and on every read door that replays it -------
#
# The wave-5 guard sits in ``build_book``, and ``build_book(allow_oversell=True)`` sends an
# oversized SELL down the 賣超 degradation branch — which performs NO multiplication. So the
# replay never raised on it, and the ``decimal.Overflow`` escaped from the SECOND consumer of
# the same rows: ``portfolio/timeseries.py``'s ``gross = t.quantity * t.price``, reached from
# ``build_dashboard``. Measured before this fix, one row, four answers: BUY -> 422 on both
# routes, SELL -> **500 「系統發生未預期的錯誤，請稍後再試」** on both. A guard that holds for
# one side of a trade is not a guard for the class.

@pytest.mark.parametrize("path", ["/api/dashboard", "/api/performance/twr"])
@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_every_replaying_read_door_answers_422_not_500_on_either_side(
    api_client: TestClient, golden_db: sqlite3.Connection, side: str, path: str
) -> None:
    """★ R-1. ``/api/performance/twr`` builds the same daily NAV series through
    ``build_dashboard``, so it inherits the dashboard's answer — and inherited the 500 too."""
    _plant_overflow_row(golden_db, side)
    client = TestClient(api_client.app, raise_server_exceptions=False)
    resp = client.get(path)
    assert resp.status_code == 422, f"HTTP {resp.status_code}: {resp.text[:300]}"
    err = resp.json()["error"]
    assert err["code"] == "unbookable_ledger"
    assert _CJK.search(str(err["message"])), err["message"]


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_the_sentence_names_the_offending_row_whichever_side_it_is(
    api_client: TestClient, golden_db: sqlite3.Connection, side: str
) -> None:
    """One row, one sentence. The owner must not have to work out that a SELL is described
    differently from a BUY — the message is what they get, and it names what to edit."""
    _plant_overflow_row(golden_db, side)
    client = TestClient(api_client.app, raise_server_exceptions=False)
    message = str(client.get("/api/dashboard").json()["error"]["message"])
    assert "2330" in message and "tw_broker" in message and "2026-02-01" in message


@pytest.mark.parametrize("path", ["/api/dashboard", "/api/performance/twr"])
def test_both_read_doors_still_serve_a_clean_ledger(
    api_client: TestClient, path: str
) -> None:
    """Control: neither route is narrowed by the guard."""
    assert api_client.get(path).status_code == 200


def test_a_clean_ledger_still_renders(api_client: TestClient) -> None:
    """Control: the golden dashboard is untouched by either change."""
    assert api_client.get("/api/dashboard").status_code == 200
