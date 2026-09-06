"""X8b — M5-06's third reader: the FX pool (換匯損益) now reads AS OF today, like the funds view.

X1 made ``portfolio/cash.py::cash_balances`` date-aware (owner ruling 2026-09-06, option C:
「今天的餘額」 counts only rows dated on or before the app clock's day) and named the reader it
did not change: ``forex/pools.py::foreign_cash_balance`` was still the WHOLE history, so after
a far-future foreign deposit the 資金 page and the 換匯損益 card disagreed on the pool by exactly
that row — and the manual's anchor ``fx.pool_equals_funds`` (§8.3, 「恆等於 §9 之營運現金池」) no
longer held. Worse than the balance: ``unrealized_fx_cash`` marked money that has not arrived
to market, a RATED future deposit moved today's ``avg_rate``, an UNRATED one pulled today's
``covered_ratio`` down (flagging exposure that is fully covered), and a future-dated
reconversion put a future realized row into 「至今已實現」.

Measured on the golden ledger (GOLDEN_NOW = 2026-06-11) before the fix, after a USD 5,000
deposit on schwab dated 2099-01-01: ``/api/cash`` schwab/USD ``0`` vs ``fx.foreign_cash``
``5000``; ``covered_ratio`` ``0.1666…`` (unrated) or ``avg_rate`` ``38.666…`` (rated, at 40);
with a 2099-01-02 reconversion USD 1,000 → TWD 34,000 on top, ``realized_fx`` ``2000``.

The fix follows ``acquisition_basis``'s existing ``as_of`` convention — ``None`` is the whole
history, the bound is inclusive — through ``foreign_cash_balance``, the unrealized-side basis
(``avg_rate`` AND ``covered_ratio`` come from one bounded call: the coverage must be computed
over the population that produced the balance it scales, or ``fx_basis_gap = cash × (1 −
ratio)`` reports a gap on money that is fully covered), and the realized sum (rows dated
``<= as_of`` only). ``build_dashboard`` passes the ``as_of`` it already has. "Today" is
``api.deps.get_now`` → ``shared.clock.app_now`` (Asia/Taipei), frozen at GOLDEN_NOW here.

The counter-evidence outranks the fix: on a ledger with no future row every FX figure is
byte-identical (the golden section below, plus a pure-function A/B), and a caller that
passes no ``as_of`` still reads the whole history.
"""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import StoredCashMovement
from portfolio_dash.forex.fx_pnl import compute_account_fx, compute_fx_summary
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Account, Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, FXConversion, Transaction

_TODAY = "2026-06-11"  # GOLDEN_NOW's date
_TODAY_D = date(2026, 6, 11)

# The golden schwab FX card, as recorded (every golden row is dated before GOLDEN_NOW).
_GOLDEN_SCHWAB_FX = {
    "avg_rate": "32", "current_spot": "33", "foreign_cash": "0",
    "foreign_stock_value": "1200", "realized_fx": "0", "unrealized_fx_stocks": "1200",
    "unrealized_fx_cash": "0", "unrealized_fx_total": "1200", "spot_delta": "1",
    "covered_ratio": "1", "fx_basis_gap": "0", "fx_basis_incomplete": False,
    "foreign_cash_negative": False,
}
_GOLDEN_ROLLUP = {"reporting_realized_fx": "0", "reporting_unrealized_fx": "1200"}

_FUTURE_USD_DEPOSIT = {
    "account_id": "schwab", "date": "2099-01-01", "kind": "deposit",
    "ccy": "USD", "amount": "5000", "note": "X8b far-future foreign deposit",
}


# --- readers -----------------------------------------------------------------------------


def _api_cash_pool(client: TestClient, account: str, ccy: str) -> str:
    body = client.get("/api/cash", params={"limit": 500}).json()
    return str(next(b["amount"] for b in body["balances"]
                    if b["account_id"] == account and b["ccy"] == ccy))


def _dashboard(client: TestClient) -> dict[str, Any]:
    body: dict[str, Any] = client.get("/api/dashboard").json()
    return body


def _schwab_fx(body: dict[str, Any]) -> dict[str, Any]:
    acct: dict[str, Any] = body["fx"]["by_account"]["schwab"]
    return acct


# --- the anchor: fx.pool_equals_funds holds again after a far-future foreign deposit -----


@pytest.mark.parametrize("acq_home_amount", [None, "200000"], ids=["unrated", "rated@40"])
def test_fx_pool_equals_funds_after_a_far_future_foreign_deposit(
    api_client: TestClient, acq_home_amount: str | None,
) -> None:
    """The manual's ``fx.pool_equals_funds`` (§8.3): 資金 page and 換匯損益 card, one pool.

    Before the fix: ``/api/cash`` ``0`` vs ``foreign_cash`` ``5000``, and the future row
    already moved today's basis — ``covered_ratio`` 0.1666… (unrated) / ``avg_rate`` 38.666…
    (rated) — so ``unrealized_fx_cash`` was priced on money that has not arrived.
    """
    body = dict(_FUTURE_USD_DEPOSIT)
    if acq_home_amount is not None:
        body["acq_home_amount"] = acq_home_amount
    assert api_client.post("/api/cash/movements", json=body).status_code == 201
    funds = _api_cash_pool(api_client, "schwab", "USD")
    assert funds == "0"                                   # the funds view is as of today
    fx = _schwab_fx(_dashboard(api_client))
    assert fx["foreign_cash"] == funds                    # ...and so is the FX pool
    # Today's basis is today's: a deposit that has not arrived funds nothing held today.
    assert fx["avg_rate"] == "32"
    assert fx["covered_ratio"] == "1"
    assert fx["fx_basis_incomplete"] is False
    assert fx["fx_basis_gap"] == "0"
    # The unrealized leg is priced on the as-of balance, so it is exactly the golden figure.
    assert fx["unrealized_fx_cash"] == _GOLDEN_SCHWAB_FX["unrealized_fx_cash"]
    assert fx["unrealized_fx_total"] == _GOLDEN_SCHWAB_FX["unrealized_fx_total"]


def test_a_far_future_reconversion_does_not_enter_realized_to_date(
    api_client: TestClient,
) -> None:
    """A reconversion dated 2099 is in the ledger, not in 「至今已實現」.

    The 2099-01-01 deposit funds the pool ON THAT DATE, so the guard accepts a 2099-01-02
    USD 1,000 → TWD 34,000 reconversion. Before the fix it produced a realized row of
    34,000 − 1,000 × 32 = 2,000 and the account reported it as realized today.
    """
    assert api_client.post("/api/cash/movements", json=_FUTURE_USD_DEPOSIT).status_code == 201
    r = api_client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2099-01-02", "from_ccy": "USD", "from_amt": "1000",
        "to_ccy": "TWD", "to_amt": "34000"})
    assert r.status_code == 201, r.json()
    body = _dashboard(api_client)
    fx = _schwab_fx(body)
    assert fx["realized_fx"] == "0"
    assert fx["foreign_cash"] == _api_cash_pool(api_client, "schwab", "USD") == "0"
    assert body["fx"]["reporting_realized_fx"] == "0"
    assert body["kpis"]["fx_realized"] == "0"


def test_a_row_dated_on_today_counts_in_both_views(api_client: TestClient) -> None:
    """The bound is inclusive (dates, not timestamps — ``acquisition_basis``'s own reading),
    so a deposit dated ON the clock's day is in today's pool on both surfaces."""
    on_today = {**_FUTURE_USD_DEPOSIT, "date": _TODAY}
    assert api_client.post("/api/cash/movements", json=on_today).status_code == 201
    funds = _api_cash_pool(api_client, "schwab", "USD")
    fx = _schwab_fx(_dashboard(api_client))
    assert funds == fx["foreign_cash"] == "5000"
    # ...and an unrated deposit that HAS arrived does dilute today's coverage (F2).
    assert Decimal(fx["covered_ratio"]) == Decimal("1000") / Decimal("6000")
    assert fx["fx_basis_incomplete"] is True


# --- counter-evidence: no future row, no digit moves --------------------------------------


def test_golden_fx_section_is_byte_identical_without_a_future_row(
    api_client: TestClient,
) -> None:
    """Every golden row is dated before GOLDEN_NOW, so the bound is a no-op: the recorded
    card is reproduced digit for digit — realized, unrealized, avg_rate, covered_ratio,
    unrealized_fx_cash and the rest — and so is the rollup."""
    body = _dashboard(api_client)
    fx = _schwab_fx(body)
    for key, recorded in _GOLDEN_SCHWAB_FX.items():
        assert fx[key] == recorded, (key, fx[key], recorded)
    for key, recorded in _GOLDEN_ROLLUP.items():
        assert body["fx"][key] == recorded, (key, body["fx"][key], recorded)
    assert body["kpis"]["fx_realized"] == "0" and body["kpis"]["fx_unrealized"] == "1200"
    assert body["fx"]["excluded_accounts"] == []


# --- the pure functions: as_of=None is the whole history; a past-only ledger is a no-op --

SCHWAB = Account(account_id="schwab", name="Schwab", broker="Schwab",
                 settlement_ccy=Currency.USD, funding_ccy=Currency.TWD,
                 dividend_model="drip_us")
AAPL = Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                  sector="Tech", name="Apple")
INSTR = {"AAPL": AAPL}
_FAR = date(2099, 1, 1)


def _conv(d: date, frm: Currency, famt: str, to: Currency, tamt: str) -> FXConversion:
    return FXConversion(account_id="schwab", date=d, from_ccy=frm, from_amount=Decimal(famt),
                        to_ccy=to, to_amount=Decimal(tamt))


def _tx(d: date, side: Side, qty: str, price: str) -> Transaction:
    return Transaction(account_id="schwab", symbol="AAPL", side=side, quantity=Decimal(qty),
                       price=Decimal(price), fees=Decimal("1"), tax=Decimal("0"),
                       trade_date=d)


def _div(d: date, net: str) -> Dividend:
    return Dividend(account_id="schwab", symbol="AAPL", date=d, type=DividendType.CASH,
                    gross=Decimal(net), withholding=Decimal("0"), net=Decimal(net))


def _mv(d: date, kind: str, amt: str, acq: str | None) -> StoredCashMovement:
    return StoredCashMovement(id=0, account_id="schwab", date=d, kind=kind, ccy=Currency.USD,
                              amount=Decimal(amt),
                              acq_home_amount=None if acq is None else Decimal(acq))


def _past_ledger() -> tuple[list[FXConversion], list[Transaction], list[Dividend],
                            list[StoredCashMovement]]:
    """Every row shape the pool reads, all dated before 2026-06-11: two acquisitions, a
    reconversion, a buy, a sell, a cash dividend, a rated opening, an unrated deposit, a
    withdrawal, and an interest credit."""
    convs = [
        _conv(date(2026, 1, 8), Currency.TWD, "320000", Currency.USD, "10000"),
        _conv(date(2026, 2, 1), Currency.TWD, "165000", Currency.USD, "5000"),
        _conv(date(2026, 3, 1), Currency.USD, "2000", Currency.TWD, "67000"),
    ]
    txs = [_tx(date(2026, 1, 10), Side.BUY, "50", "100"),
           _tx(date(2026, 4, 1), Side.SELL, "10", "120")]
    divs = [_div(date(2026, 5, 1), "70")]
    moves = [_mv(date(2026, 1, 5), "OPENING", "3000", "96000"),
             _mv(date(2026, 2, 10), "DEPOSIT", "1000", None),
             _mv(date(2026, 3, 15), "WITHDRAW", "500", None),
             _mv(date(2026, 4, 20), "INTEREST", "3.25", None)]
    return convs, txs, divs, moves


def _future_rows() -> tuple[list[FXConversion], list[Transaction], list[Dividend],
                            list[StoredCashMovement]]:
    """The same shapes, dated 2099 — a rated acquisition, a reconversion, a buy, a
    dividend, an unrated deposit."""
    convs = [_conv(_FAR, Currency.TWD, "400000", Currency.USD, "10000"),
             _conv(date(2099, 1, 2), Currency.USD, "1000", Currency.TWD, "40000")]
    txs = [_tx(_FAR, Side.BUY, "10", "100")]
    divs = [_div(_FAR, "50")]
    moves = [_mv(_FAR, "DEPOSIT", "5000", None)]
    return convs, txs, divs, moves


def _fields(r: Any) -> dict[str, str]:
    """Every wire field as its string form — byte identity, not numeric equality."""
    return {k: str(v) for k, v in r.model_dump().items()}


def test_every_figure_is_byte_identical_on_a_past_only_ledger_with_and_without_the_bound(
) -> None:
    """The reverse proof: with no future row, ``as_of`` changes NOTHING — realized,
    unrealized (both legs + total), avg_rate, covered_ratio, gap, flags — as strings."""
    convs, txs, divs, moves = _past_ledger()
    unbounded = compute_account_fx(SCHWAB, Currency.USD, Decimal("4800"), txs, divs, convs,
                                   INSTR, spot=Decimal("33"), movements=moves)
    bounded = compute_account_fx(SCHWAB, Currency.USD, Decimal("4800"), txs, divs, convs,
                                 INSTR, spot=Decimal("33"), movements=moves, as_of=_TODAY_D)
    assert _fields(bounded) == _fields(unbounded)
    # The ledger really exercises every branch: a basis, a reconversion, a partial coverage.
    assert unbounded.realized_fx not in (None, Decimal("0"))
    assert unbounded.covered_ratio != Decimal("1")
    assert unbounded.unrealized_fx_cash is not None


def test_as_of_none_is_still_the_whole_history() -> None:
    """A caller that passes nothing gets the pre-fix reading, future rows included — the
    same ``None`` semantics ``acquisition_basis`` and ``cash_balances`` give it."""
    p_convs, p_txs, p_divs, p_moves = _past_ledger()
    f_convs, f_txs, f_divs, f_moves = _future_rows()
    convs, txs, divs, moves = (p_convs + f_convs, p_txs + f_txs, p_divs + f_divs,
                               p_moves + f_moves)
    past_only = compute_account_fx(SCHWAB, Currency.USD, Decimal("4800"), p_txs, p_divs,
                                   p_convs, INSTR, spot=Decimal("33"), movements=p_moves)
    whole = compute_account_fx(SCHWAB, Currency.USD, Decimal("4800"), txs, divs, convs,
                               INSTR, spot=Decimal("33"), movements=moves)
    explicit_none = compute_account_fx(SCHWAB, Currency.USD, Decimal("4800"), txs, divs,
                                       convs, INSTR, spot=Decimal("33"), movements=moves,
                                       as_of=None)
    assert _fields(whole) == _fields(explicit_none)
    # ...and the whole history genuinely contains the 2099 rows: +10,000 −1,000 −1,001
    # +50 +5,000 on the cash, a second realized row, a moved average and coverage.
    assert whole.foreign_cash == past_only.foreign_cash + Decimal("13049")
    assert whole.realized_fx is not None and past_only.realized_fx is not None
    assert whole.realized_fx != past_only.realized_fx
    assert whole.avg_rate != past_only.avg_rate
    assert whole.covered_ratio != past_only.covered_ratio


def test_bounded_to_today_a_ledger_with_future_rows_reads_as_its_past_only_self() -> None:
    """The fix, at the function level: bounding to today makes the mixed ledger report
    exactly what the past-only ledger reports — every field, as strings."""
    p_convs, p_txs, p_divs, p_moves = _past_ledger()
    f_convs, f_txs, f_divs, f_moves = _future_rows()
    past_only = compute_account_fx(SCHWAB, Currency.USD, Decimal("4800"), p_txs, p_divs,
                                   p_convs, INSTR, spot=Decimal("33"), movements=p_moves)
    bounded = compute_account_fx(
        SCHWAB, Currency.USD, Decimal("4800"), p_txs + f_txs, p_divs + f_divs,
        p_convs + f_convs, INSTR, spot=Decimal("33"), movements=p_moves + f_moves,
        as_of=_TODAY_D)
    assert _fields(bounded) == _fields(past_only)


def test_compute_fx_summary_threads_as_of_and_defaults_to_the_whole_history() -> None:
    """The rollup entry point ``build_dashboard`` calls: ``as_of`` reaches every account;
    omitting it is the whole history."""
    p_convs, p_txs, p_divs, p_moves = _past_ledger()
    f_convs, f_txs, f_divs, f_moves = _future_rows()
    exposure = {"schwab": (Currency.USD, Decimal("4800"))}

    def spot(frm: Currency, to: Currency) -> Decimal:
        return Decimal("1") if frm is to else Decimal("33")

    whole = compute_fx_summary({"schwab": SCHWAB}, INSTR, p_txs + f_txs, p_divs + f_divs,
                               p_convs + f_convs, exposure, spot, Currency.TWD,
                               p_moves + f_moves)
    bounded = compute_fx_summary({"schwab": SCHWAB}, INSTR, p_txs + f_txs, p_divs + f_divs,
                                 p_convs + f_convs, exposure, spot, Currency.TWD,
                                 p_moves + f_moves, as_of=_TODAY_D)
    past_only = compute_fx_summary({"schwab": SCHWAB}, INSTR, p_txs, p_divs, p_convs,
                                   exposure, spot, Currency.TWD, p_moves)
    assert _fields(bounded.by_account["schwab"]) == _fields(past_only.by_account["schwab"])
    assert str(bounded.reporting_realized_fx) == str(past_only.reporting_realized_fx)
    assert str(bounded.reporting_unrealized_fx) == str(past_only.reporting_unrealized_fx)
    assert whole.by_account["schwab"].foreign_cash != past_only.by_account["schwab"].foreign_cash


def test_a_pool_whose_only_basis_is_in_the_future_has_no_basis_today() -> None:
    """A rate that will exist in 2099 is not a rate today: bounded, the account reports
    None on both sides (the existing 「never guess a rate」 convention), not a figure priced
    at a future acquisition's rate."""
    convs = [_conv(_FAR, Currency.TWD, "400000", Currency.USD, "10000")]
    r = compute_account_fx(SCHWAB, Currency.USD, Decimal("0"), [], [], convs, INSTR,
                           spot=Decimal("33"), as_of=_TODAY_D)
    assert r.avg_rate is None and r.realized_fx is None and r.unrealized_fx_total is None
    assert r.foreign_cash == Decimal("0")
    # Unbounded, the same ledger has a (future) basis — the old reading, on request.
    assert compute_account_fx(SCHWAB, Currency.USD, Decimal("0"), [], [], convs, INSTR,
                              spot=Decimal("33")).avg_rate == Decimal("40")
