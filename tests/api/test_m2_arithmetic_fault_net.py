"""N-1 — the never-500 rule needs a net for the CLASS, not one guard per consumer.

Waves 5 and 9 re-typed ``ArithmeticError`` into ``UnbookableLedgerError`` inside the two
modules that own the replay (``portfolio/cost_basis.py::build_book`` and
``portfolio/timeseries.py``), and wave 3 registered a 422 handler for that type. That closes
the fault wherever the REPLAY is the consumer — and only there. A ledger quantity is also
multiplied at three sites neither owner can see, so one direct-SQL row with
``quantity='1E+999999'`` still produced an anonymous 500. Measured before this fix, one row,
sixteen combinations (2 accounts x 2 sides x 4 routes), **ten of them 500**, every one a
``decimal.Overflow``:

* ``forex/pools.py:224 foreign_cash_balance`` <- ``fx_pnl.py:150 compute_account_fx`` <-
  ``fx_pnl.py:296 compute_fx_summary`` -> ``GET /api/dashboard`` and
  ``GET /api/performance/twr`` **for an account whose settlement currency differs from its
  funding currency** (``schwab`` / ``moomoo_my``), on the **SELL** shape only;
* ``portfolio/cash.py:119/121 cash_balances`` <- ``api/routers/cash.py:101`` ->
  ``GET /api/cash``, both sides, every account;
* ``api/routers/ledgers.py:172`` (``gross = t.quantity * t.price``, in the ROUTER) ->
  ``GET /api/ledgers/transactions``, both sides.

Two facts make a per-consumer guard the wrong shape. The escape moves with the account's
CURRENCY and with the SIDE of the trade, so a guard proven on ``tw_broker``/BUY says nothing
about ``schwab``/SELL on the same route; and the third site multiplies in the router itself,
where there is no lower layer to fix. Every future consumer of a ledger quantity would need
its own ``try``. The one thing all ten share is the API boundary — so the class is answered
there, once, and the two replay owners keep precedence for the sentence that names the row.
"""

import logging
import re
import sqlite3
from collections.abc import Callable, Iterator
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api.errors import register_error_handlers
from portfolio_dash.portfolio.cost_basis import _UNCOMPUTABLE_ZH, UnbookableLedgerError

_ASCII_ONLY = re.compile(r"^[\x00-\x7f]*$")
_CJK = re.compile(r"[一-鿿]")


@pytest.fixture
def raising_client() -> Iterator[Callable[[Exception], TestClient]]:
    """A one-route app wired to the REAL handlers, whose route raises *exc*.

    Same shape (and the same socket note) as ``tests/api/test_r5_error_envelope_zh.py`` and
    ``test_m2_never_500_ledger_errors.py``: sockets are re-enabled because the TestClient's
    anyio portal uses a real self-pipe on Windows. Nothing leaves the process.
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


def _real_overflow() -> ArithmeticError:
    """The ACTUAL fault the ledger produces, not a hand-constructed stand-in.

    ``decimal.Overflow`` is trapped by default, and it is a SIBLING of
    ``decimal.InvalidOperation`` rather than a subclass — the hierarchy fact that has already
    let this class walk past three narrow guards.
    """
    try:
        Decimal("1E+999999") * Decimal("600")
    except ArithmeticError as exc:
        return exc
    raise AssertionError("expected a decimal fault from 1E+999999 * 600")


# --- the handler itself ---------------------------------------------------------------

def test_a_decimal_overflow_answers_422_with_the_generic_sentence(
    raising_client: Callable[[Exception], TestClient],
) -> None:
    """★ The escape from any of the three consumers, answered once at the boundary."""
    err = _error(raising_client(_real_overflow()), 422)
    assert err["code"] == "unbookable_ledger"
    assert err["message"] == _UNCOMPUTABLE_ZH


def test_a_zero_division_is_the_same_class_not_a_special_case(
    raising_client: Callable[[Exception], TestClient],
) -> None:
    """``ZeroDivisionError`` is an ``ArithmeticError`` with no ``decimal`` in its ancestry.

    The handler is registered on the CLASS precisely so a fourth consumer dividing by a zero
    share count is covered without anybody remembering to add a fourth arm.
    """
    err = _error(raising_client(ZeroDivisionError("division by zero")), 422)
    assert err["code"] == "unbookable_ledger"
    assert err["message"] == _UNCOMPUTABLE_ZH


def test_the_exception_detail_never_reaches_the_owners_toast(
    raising_client: Callable[[Exception], TestClient],
) -> None:
    """``message`` is rendered VERBATIM as a red toast, so it is the constant, never
    ``str(exc)`` — which for a trapped ``decimal.Overflow`` is a repr of the signal list."""
    message = str(_error(raising_client(ZeroDivisionError("division by zero")), 422)["message"])
    assert not _ASCII_ONLY.match(message), message
    assert _CJK.search(message), message
    assert "division" not in message and "decimal" not in message, message


def test_the_traceback_is_still_recorded(
    raising_client: Callable[[Exception], TestClient],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """★ A ``ZeroDivisionError`` from a genuine programming bug lands here too, so the
    diagnosis must NOT be lost. Starlette's ``ExceptionMiddleware`` does not re-raise what a
    handler answered — unlike ``ServerErrorMiddleware``, which re-raises after the catch-all
    — so this handler logs it itself, exactly as the catch-all does."""
    with caplog.at_level(logging.ERROR, logger="portfolio_dash.api.errors"):
        _error(raising_client(_real_overflow()), 422)
    records = [r for r in caplog.records if r.name == "portfolio_dash.api.errors"]
    assert records, "the arithmetic handler logged nothing"
    assert records[0].levelno == logging.ERROR
    assert records[0].exc_info is not None, "logged without the traceback"


# --- precedence: the replay owners keep their precise sentence --------------------------

def test_an_unbookable_ledger_error_still_answers_with_its_own_precise_sentence(
    raising_client: Callable[[Exception], TestClient],
) -> None:
    """★ ``UnbookableLedgerError`` is a ``ValueError``, so ``ArithmeticError`` is NOT in its
    MRO and Starlette's most-specific-registered-class dispatch cannot reach the new arm.

    This is the whole point of re-typing in the two replay owners: they know WHICH row is
    uncomputable, and the generic sentence would throw that away.
    """
    precise = ("2330（tw_broker）於 2026-02-01 的帳本資料數值無法計算"
               "（數值過大或除以零）— 請於交易帳本修正該列後重試")
    err = _error(raising_client(UnbookableLedgerError(precise)), 422)
    assert err["code"] == "unbookable_ledger"
    assert err["message"] == precise
    assert err["message"] != _UNCOMPUTABLE_ZH


# --- the catch-all is still the catch-all ----------------------------------------------

def test_a_plain_runtime_error_is_still_an_internal_error(
    raising_client: Callable[[Exception], TestClient],
) -> None:
    """Control: the net narrows nothing. A non-arithmetic fault is still a real fault, and
    answering 422「請修正帳本」 for it would send the owner to edit a ledger that is fine."""
    err = _error(raising_client(RuntimeError("boom")), 500)
    assert err["code"] == "internal_error"
    assert "boom" not in str(err["message"])


# --- end to end, on a real ledger ------------------------------------------------------
#
# The row is planted by direct SQL because every write door validates — the same repro
# `tests/api/test_m2_never_500_ledger_errors.py` and
# `tests/contract/test_m2_negative_quantity_never_500.py` use.

def _plant_overflow_row(conn: sqlite3.Connection, *, account_id: str, symbol: str,
                        side: str, price: str) -> None:
    conn.execute(
        "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax,"
        " trade_date) VALUES (?,?,?,'1E+999999',?,'0','0','2026-02-01')",
        (account_id, symbol, side, price))
    conn.commit()


def _read(api_client: TestClient, path: str) -> tuple[int, dict[str, object]]:
    client = TestClient(api_client.app, raise_server_exceptions=False)
    resp = client.get(path)
    payload = resp.json()
    err = payload.get("error", {}) if isinstance(payload, dict) else {}
    assert isinstance(err, dict)
    return resp.status_code, err


@pytest.mark.parametrize("path", ["/api/dashboard", "/api/performance/twr"])
def test_a_foreign_currency_account_no_longer_500s_on_a_sell(
    api_client: TestClient, golden_db: sqlite3.Connection, path: str
) -> None:
    """★ The gap wave 9 measured and could not reach: ``schwab``'s settlement currency (USD)
    differs from its funding currency (TWD), so ``build_dashboard`` runs ``compute_fx_summary``
    — which multiplies the same quantity in ``forex/pools.py`` — BEFORE the trend builder whose
    guard wave 9 added. Same route, same row, opposite answer depending only on the account.
    """
    _plant_overflow_row(golden_db, account_id="schwab", symbol="AAPL", side="SELL",
                        price="120")
    status, err = _read(api_client, path)
    assert status == 422, f"HTTP {status}: {err}"
    assert err["code"] == "unbookable_ledger"
    assert _CJK.search(str(err["message"])), err["message"]


@pytest.mark.parametrize("path", ["/api/cash", "/api/ledgers/transactions"])
@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_the_cash_and_transaction_read_doors_answer_422_on_either_side(
    api_client: TestClient, golden_db: sqlite3.Connection, side: str, path: str
) -> None:
    """★ ``/api/cash`` multiplies in ``portfolio/cash.py`` and ``/api/ledgers/transactions``
    multiplies in the ROUTER — neither has a replay owner to re-type the fault, and both
    500'd for a BUY as well as a SELL (so this is pre-existing, not a wave-5 residual)."""
    _plant_overflow_row(golden_db, account_id="tw_broker", symbol="2330", side=side,
                        price="600")
    status, err = _read(api_client, path)
    assert status == 422, f"HTTP {status}: {err}"
    assert err["code"] == "unbookable_ledger"
    assert _CJK.search(str(err["message"])), err["message"]


@pytest.mark.parametrize("path", ["/api/dashboard", "/api/performance/twr", "/api/cash",
                                  "/api/ledgers/transactions"])
def test_every_touched_route_still_serves_a_clean_ledger(
    api_client: TestClient, path: str
) -> None:
    """Control: a boundary handler cannot change what a healthy request answers."""
    assert api_client.get(path).status_code == 200
