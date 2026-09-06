"""R1/③ counter-evidence: the sell preview must mirror the replay's BRANCHES.

``_position_preview``'s own docstring claims it "replicates build_book's OWN sell arithmetic
exactly ... so the preview equals the booked realized row bit-for-bit".  That was true of the
ORDINARY branch it was written against, and only of that one.  ``build_book`` grew two more
sell branches — the declared short (2026-07-31) and the acked oversell — and each of them
deliberately emits **no realized row**.  The preview kept applying the ordinary formula to
all three, so it invented a realized P&L for trades the ledger books nothing for, while the
holdings column two rows above honestly printed 「—」.

The parametrised cross-check at the bottom is the real guard: it replays the hypothetical
transaction through ``build_book`` and demands the preview agree with what was actually
booked, for every branch.  A fourth branch added later fails it automatically.
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.api.routers.input_center import ManualBody, _position_preview
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    insert_transaction,
    load_ledger_bundle,
    upsert_instrument,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.results import RealizedRow
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory

D = Decimal
_TSLA = Instrument(symbol="TSLA", market=Market.US, quote_ccy=Currency.USD,
                   sector="Auto", name="Tesla")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    seed_accounts(conn)
    upsert_instrument(conn, _TSLA)
    return conn


def _tx(conn: sqlite3.Connection, side: Side, qty: str, price: str, d: date, *,
        short: bool = False, fee: str = "0", tax: str = "0") -> None:
    insert_transaction(conn, account_id="schwab", symbol="TSLA", side=side,
                       quantity=D(qty), price=D(price), fees=D(fee), tax=D(tax),
                       trade_date=d, short_sale=short)


def _body(qty: str, price: str, *, short: bool = False) -> ManualBody:
    return ManualBody(account_id="schwab", symbol="TSLA", side="SELL",
                      date=date(2026, 8, 1), shares=D(qty), price=D(price),
                      short_sale=short)


def _preview(conn: sqlite3.Connection, body: ManualBody) -> dict[str, object]:
    out = _position_preview(conn, body, D("0"), D("0"), body.shares * body.price)
    assert out is not None
    return out


def _booked_realized(conn: sqlite3.Connection, body: ManualBody) -> Decimal | None:
    """Replay the ledger WITH the hypothetical row and return the realized it actually books.

    ``None`` when the branch books no realized row at all — which is the whole point.
    """
    before = {id(r) for r in build_book(load_ledger_bundle(conn),
                                        allow_oversell=True).realized.rows}
    _tx(conn, Side.SELL, str(body.shares), str(body.price), body.date,
        short=body.short_sale)
    rows = [r for r in build_book(load_ledger_bundle(conn), allow_oversell=True).realized.rows
            if r.sell_date == body.date]
    assert not before & {id(r) for r in rows}
    if not rows:
        return None
    return sum((r.realized for r in rows), D("0"))


# --- the three branches, stated one at a time -------------------------------------------

def test_extending_an_open_short_fabricates_no_realized_pnl() -> None:
    """Short 50 already open; declare another sell of 50.

    ``build_book`` takes ``from_long = min(qty, max(shares, 0)) = 0`` — nothing realizes,
    the whole 50 extends the short lot.  The old preview divided by the position's NEGATIVE
    share count, which flipped ``cost_removed`` positive and printed a profit on a trade that
    books no realized row.
    """
    conn = _conn()
    _tx(conn, Side.BUY, "100", "240", date(2026, 4, 1))
    _tx(conn, Side.SELL, "150", "260", date(2026, 6, 10), short=True)   # 50 short open
    pp = _preview(conn, _body("50", "300", short=True))
    assert pp["realized_pnl"] is None, f"fabricated a realized P&L: {pp['realized_pnl']}"
    assert pp["cost_removed"] is None
    assert pp["realized_shares"] == "0"
    assert pp["short_opened"] == "50"
    conn.close()


def test_undeclared_oversell_fabricates_no_realized_pnl() -> None:
    """Hold 100, sell 150 undeclared: the replay discards the basis and books nothing (待釐清)."""
    conn = _conn()
    _tx(conn, Side.BUY, "100", "240", date(2026, 4, 1))
    pp = _preview(conn, _body("150", "300"))
    assert pp["realized_pnl"] is None, f"fabricated a realized P&L: {pp['realized_pnl']}"
    assert pp["cost_removed"] is None
    assert pp["oversell"] is True
    conn.close()


def test_declared_sell_realizes_only_the_long_portion() -> None:
    """Hold 100, declare a sell of 150: 100 realize normally, 50 open a short."""
    conn = _conn()
    _tx(conn, Side.BUY, "100", "240", date(2026, 4, 1))
    pp = _preview(conn, _body("150", "300", short=True))
    # per_share_net = 300 (no fees); the long 100 realize (300-240)*100 = 6,000.
    assert pp["realized_shares"] == "100"
    assert pp["short_opened"] == "50"
    assert Decimal(str(pp["realized_pnl"])) == D("6000")
    conn.close()


def test_ordinary_sell_is_byte_identical_to_before() -> None:
    """The branch the original docstring was written against must not move."""
    conn = _conn()
    _tx(conn, Side.BUY, "100", "240", date(2026, 4, 1))
    pp = _preview(conn, _body("40", "300"))
    assert Decimal(str(pp["cost_removed"])) == D("9600")      # 24,000 * (40/100)
    assert Decimal(str(pp["realized_pnl"])) == D("2400")      # 12,000 - 9,600
    assert pp["remain_shares"] == "60"
    assert pp["realized_shares"] == "40"
    assert pp["short_opened"] is None and pp["oversell"] is False
    conn.close()


# --- the guard that makes a fourth branch fail on its own --------------------------------

@pytest.mark.parametrize(
    ("setup", "qty", "price", "short"),
    [
        pytest.param([("BUY", "100", "240")], "40", "300", False, id="ordinary"),
        pytest.param([("BUY", "100", "240")], "100", "300", False, id="full-exit"),
        pytest.param([("BUY", "100", "240")], "150", "300", False, id="oversell"),
        pytest.param([("BUY", "100", "240")], "150", "300", True, id="declared-partial"),
        pytest.param([("BUY", "100", "240"), ("SHORT", "150", "260")],
                     "50", "300", True, id="short-extension"),
        pytest.param([("BUY", "100", "240"), ("SHORT", "150", "260")],
                     "50", "300", False, id="undeclared-into-a-short"),
        # QA-08: a declared short opened from a FLAT position — nothing held at all. The
        # only one of the eleven trade shapes the preview refused to project, because the
        # SELL arm returned None on `held is None` BEFORE the `short_sale` branch was
        # reached. The ledger books it like any other declared short.
        pytest.param([], "30", "300", True, id="declared-short-from-flat"),
    ],
)
def test_preview_realized_equals_the_realized_the_ledger_actually_books(
    setup: list[tuple[str, str, str]], qty: str, price: str, short: bool
) -> None:
    conn = _conn()
    for kind, q, p in setup:
        _tx(conn, Side.BUY if kind == "BUY" else Side.SELL, q, p, date(2026, 4, 1),
            short=(kind == "SHORT"))
    body = _body(qty, price, short=short)
    pp = _preview(conn, body)
    booked = _booked_realized(conn, body)
    shown = None if pp["realized_pnl"] is None else Decimal(str(pp["realized_pnl"]))
    assert shown == booked, f"preview {shown} != booked {booked}"
    conn.close()


# --- the BUY side of the same seam: a buy against an open short COVERS it ----------------

def _buy_body(qty: str, price: str) -> ManualBody:
    return ManualBody(account_id="schwab", symbol="TSLA", side="BUY",
                      date=date(2026, 8, 1), shares=D(qty), price=D(price))


def _booked_cover(conn: sqlite3.Connection, body: ManualBody) -> Decimal | None:
    _tx(conn, Side.BUY, str(body.shares), str(body.price), body.date)
    rows = [r for r in build_book(load_ledger_bundle(conn), allow_oversell=True).realized.rows
            if r.sell_date == body.date]
    return sum((r.realized for r in rows), D("0")) if rows else None


@pytest.mark.parametrize(
    ("qty", "price", "expect_new_shares"),
    [
        pytest.param("30", "200", "-20", id="partial-cover-still-short"),
        pytest.param("50", "200", "0", id="exact-cover-flat"),
        pytest.param("80", "200", "30", id="over-cover-goes-long"),
    ],
)
def test_buy_against_an_open_short_covers_and_matches_the_booked_realized(
    qty: str, price: str, expect_new_shares: str
) -> None:
    """Short 50 open at 260; a buy at 200 covers it and books a realized short_cover row.

    The old buy branch averaged the short's (negative) proceeds together with the buy's cost
    — a quantity the ledger never holds — and its exact-cover case divided by zero, which the
    catch-all turned into a blank preview rather than 「已回補，目前空手」.
    """
    conn = _conn()
    _tx(conn, Side.BUY, "100", "240", date(2026, 4, 1))
    _tx(conn, Side.SELL, "150", "260", date(2026, 6, 10), short=True)   # 50 short @ 260
    body = _buy_body(qty, price)
    pp = _preview(conn, body)
    assert pp["new_shares"] == expect_new_shares
    booked = _booked_cover(conn, body)
    assert Decimal(str(pp["realized_pnl"])) == booked
    conn.close()


def test_exact_cover_states_no_average_rather_than_returning_a_blank_preview() -> None:
    conn = _conn()
    _tx(conn, Side.BUY, "100", "240", date(2026, 4, 1))
    _tx(conn, Side.SELL, "150", "260", date(2026, 6, 10), short=True)
    pp = _preview(conn, _buy_body("50", "200"))
    assert pp["new_shares"] == "0"
    assert pp["new_original_avg"] is None and pp["new_adjusted_avg"] is None
    assert Decimal(str(pp["realized_pnl"])) == D("3000")     # (260 - 200) * 50
    conn.close()


def test_ordinary_buy_is_byte_identical_to_before() -> None:
    conn = _conn()
    _tx(conn, Side.BUY, "100", "240", date(2026, 4, 1))
    pp = _preview(conn, _buy_body("100", "260"))
    assert pp["new_shares"] == "200"
    assert Decimal(str(pp["new_original_avg"])) == D("250")
    assert "covered_shares" not in pp
    conn.close()


# --- F-01 (full-site interaction sweep, 2026-08-27): the column beside the one above -----
#
# The guard above pins `preview.realized ≡ the booked realized row` for every branch. It
# pins the one column that was NOT broken twice. The average pair rendered next to it still
# projects old → old on all three branches, because the SELL side returns no `new_*_avg` at
# all and `web/input.js` states the omission as a fact — "A SELL leaves the averages
# unchanged". That is true of the ORDINARY branch and of no other:
#
#   * a DECLARED short that consumes the long lot leaves a SHORT position whose basis is the
#     proceeds received, so the average becomes the average SALE price;
#   * an UNDECLARED oversell DISCARDS the basis (`pos.original_total = _ZERO`), so the
#     average collapses to zero — and the preview printed the PRE-trade average directly
#     above its own warning that the basis will be permanently discarded;
#   * a FULL exit leaves no position at all (`build_book` drops `shares == 0`), so there is
#     no average to show — the convention the BUY branch's exact-cover case already uses.


def _booked_avgs(
    conn: sqlite3.Connection, body: ManualBody
) -> tuple[Decimal | None, Decimal | None]:
    """Replay the ledger WITH the hypothetical row; return the position's (original,
    adjusted) averages afterwards, or (None, None) when it holds no position at all.

    Asked of ``build_book`` rather than hand-computed, for the same reason
    :func:`_booked_realized` is: a hand-computed expectation can agree with a wrong preview.
    """
    _tx(conn, Side.SELL, str(body.shares), str(body.price), body.date,
        short=body.short_sale)
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    for h in book.holdings:
        if h.account_id == "schwab" and h.symbol == "TSLA":
            return h.original_avg, h.adjusted_avg
    return None, None


@pytest.mark.parametrize(
    ("setup", "qty", "price", "short"),
    [
        pytest.param([("BUY", "100", "240")], "40", "300", False, id="ordinary"),
        pytest.param([("BUY", "100", "240")], "100", "300", False, id="full-exit"),
        pytest.param([("BUY", "100", "240")], "150", "300", False, id="oversell"),
        pytest.param([("BUY", "100", "240")], "150", "300", True, id="declared-partial"),
        pytest.param([("BUY", "100", "240"), ("SHORT", "150", "260")],
                     "50", "300", True, id="short-extension"),
        pytest.param([("BUY", "100", "240"), ("SHORT", "150", "260")],
                     "50", "300", False, id="undeclared-into-a-short"),
        # QA-08: a declared short opened from a FLAT position — nothing held at all. The
        # only one of the eleven trade shapes the preview refused to project, because the
        # SELL arm returned None on `held is None` BEFORE the `short_sale` branch was
        # reached. The ledger books it like any other declared short.
        pytest.param([], "30", "300", True, id="declared-short-from-flat"),
    ],
)
def test_preview_new_average_equals_the_average_the_ledger_actually_books(
    setup: list[tuple[str, str, str]], qty: str, price: str, short: bool
) -> None:
    conn = _conn()
    for kind, q, p in setup:
        _tx(conn, Side.BUY if kind == "BUY" else Side.SELL, q, p, date(2026, 4, 1),
            short=(kind == "SHORT"))
    body = _body(qty, price, short=short)
    pp = _preview(conn, body)
    booked_orig, booked_adj = _booked_avgs(conn, body)
    for key, booked in (("new_original_avg", booked_orig), ("new_adjusted_avg", booked_adj)):
        shown_raw = pp.get(key)
        shown = None if shown_raw is None else Decimal(str(shown_raw))
        assert shown == booked, f"{key}: preview {shown!r} != booked {booked!r}"
    conn.close()


def test_a_declared_short_projects_the_average_sale_price_not_the_old_cost() -> None:
    """Hold 100 @240, declare a sell of 200 @310: the long goes, a 100-share short opens.

    The short's basis is the proceeds received, so the projected average is the per-share
    NET sale price — not the 240 the position used to cost. Stated as a literal so the
    identity above cannot pass by agreeing with a second wrong derivation.
    """
    conn = _conn()
    _tx(conn, Side.BUY, "100", "240", date(2026, 4, 1))
    pp = _preview(conn, _body("200", "310", short=True))
    assert pp["remain_shares"] == "-100"
    assert Decimal(str(pp["new_original_avg"])) == D("310")   # per_share_net, no fees here
    assert Decimal(str(pp["new_adjusted_avg"])) == D("310")
    conn.close()


def test_an_undeclared_oversell_projects_the_discarded_basis_not_the_old_average() -> None:
    """The card must not print 「240.00 → 240.00」 above a warning that says the basis is gone."""
    conn = _conn()
    _tx(conn, Side.BUY, "100", "240", date(2026, 4, 1))
    pp = _preview(conn, _body("150", "300"))
    assert pp["oversell"] is True
    assert Decimal(str(pp["new_original_avg"])) == D("0")
    assert Decimal(str(pp["new_adjusted_avg"])) == D("0")
    conn.close()


def test_a_full_exit_shows_no_average_because_there_is_no_position() -> None:
    conn = _conn()
    _tx(conn, Side.BUY, "100", "240", date(2026, 4, 1))
    pp = _preview(conn, _body("100", "300"))
    assert pp["remain_shares"] == "0"
    assert pp["new_original_avg"] is None and pp["new_adjusted_avg"] is None
    conn.close()


# --- QA-08 (2026-08-29): the ELEVENTH shape — a declared short opened from FLAT ----------
#
# `_position_preview`'s SELL arm returned None on `held is None` before `body.short_sale`
# was ever consulted, so the one trade shape that creates a position out of nothing was the
# one shape with no projection at all. Nothing wrong was shown; the card was simply blank
# where the app is otherwise complete — and blank is what a preview looks like when the
# thing it is previewing is the branch nobody wired.


def test_a_declared_short_from_a_flat_position_is_projected_not_withheld() -> None:
    """Nothing held; declare a sell of 30 @ 300. from_long = 0, the whole 30 opens a short.

    The projected basis is the proceeds received, so the average IS the per-share net sale
    price — a negative total over a negative share count, exactly as the replay reports it.
    """
    conn = _conn()
    pp = _preview(conn, _body("30", "300", short=True))
    assert pp["kind"] == "sell"
    assert pp["remain_shares"] == "-30"
    assert pp["realized_shares"] == "0"
    assert pp["short_opened"] == "30"
    assert pp["oversell"] is False
    # Nothing was held, so nothing realizes and nothing is removed — not a zero, an absence.
    assert pp["realized_pnl"] is None and pp["cost_removed"] is None
    assert Decimal(str(pp["new_original_avg"])) == D("300")
    assert Decimal(str(pp["new_adjusted_avg"])) == D("300")
    # A fresh position has no OLD side to compare against.
    assert pp["old_shares"] is None and pp["old_original_avg"] is None
    assert pp["note"] is not None and "空單" in str(pp["note"])
    conn.close()


def _booked_position(conn: sqlite3.Connection) -> tuple[Decimal, Decimal, Decimal] | None:
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    for h in book.holdings:
        if (h.account_id, h.symbol) == ("schwab", "TSLA"):
            return h.shares, h.original_avg, h.adjusted_avg
    return None


def test_the_flat_declared_short_projection_equals_what_the_ledger_books() -> None:
    """The projection, then the same trade replayed — share count and both averages."""
    conn = _conn()
    body = _body("30", "300", short=True)
    pp = _preview(conn, body)
    _tx(conn, Side.SELL, "30", "300", body.date, short=True)
    booked = _booked_position(conn)
    assert booked is not None
    shares, orig_avg, adj_avg = booked
    assert Decimal(str(pp["remain_shares"])) == shares
    assert Decimal(str(pp["new_original_avg"])) == orig_avg
    assert Decimal(str(pp["new_adjusted_avg"])) == adj_avg
    conn.close()


def test_qa08_through_the_real_door_with_the_real_fee_engine(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The QA-08 repro end to end: `/api/input/manual/preview` then `/commit`, same numbers.

    Schwab SELL 30 AAPL @ 200 declared short, nothing held. The real engine charges
    SEC 0.12 + TAF 0.01 = 0.13, so the short's basis is −5,999.87 and its average sale price
    is 5,999.87 / 30. Compared against what the dashboard reports AFTER the commit rather
    than against hand-typed constants alone, because a constant can agree with a wrong
    preview.
    """
    def seed(conn: sqlite3.Connection) -> None:
        seed_accounts(conn)
        upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US,
                                           quote_ccy=Currency.USD, sector="Tech",
                                           name="Apple"))

    client = dashboard_client_factory(seed)
    body = {"account_id": "schwab", "symbol": "AAPL", "date": "2026-03-01",
            "side": "SELL", "shares": "30", "price": "200", "short_sale": True}
    preview = client.post("/api/input/manual/preview", json=body)
    assert preview.status_code == 200, preview.text
    payload = preview.json()
    assert payload["fee"] == "0.13" and payload["tax"] == "0"
    pp = payload["position_preview"]
    assert pp is not None, "QA-08: the only trade shape with no projection at all"
    assert pp["remain_shares"] == "-30"
    assert pp["realized_pnl"] is None and pp["short_opened"] == "30"

    commit = client.post("/api/input/manual/commit", json=body)
    assert commit.status_code == 201, commit.text
    holding = next(h for h in client.get("/api/dashboard").json()["holdings"]
                   if h["symbol"] == "AAPL" and h["account_id"] == "schwab")
    assert holding["short_open"] is True
    assert Decimal(pp["remain_shares"]) == Decimal(holding["shares"])
    assert Decimal(pp["new_original_avg"]) == Decimal(holding["original_avg"])
    assert Decimal(pp["new_adjusted_avg"]) == Decimal(holding["adjusted_avg"])
    # …and the basis the QA run measured: −(30 × 200 − 0.13) = −5,999.87.
    #
    # Quantized, because the STORED value carries the 28-significant-digit residue of the
    # replay's own ``(gross − fee − tax) / qty × qty`` round trip
    # (−5999.870000000000000000000001), and the preview reproduces that residue exactly
    # because it uses the same operands in the same order. That is the whole reason every
    # other assertion above compares the preview against the REPLAY rather than against a
    # typed constant: a constant would have pinned a number neither surface produces.
    assert Decimal(holding["original_cost_total"]).quantize(D("0.01")) == D("-5999.87")


# --- the BUY side, every projected column (the mirror obligation one arm over) ------------


def _booked_buy_state(
    conn: sqlite3.Connection, body: ManualBody
) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal, Decimal]:
    """Replay WITH the hypothetical buy: (shares, orig_avg, adj_avg, realized, covered)."""
    _tx(conn, Side.BUY, str(body.shares), str(body.price), body.date)
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    covers = [r for r in book.realized.rows if r.sell_date == body.date]
    held = next((h for h in book.holdings
                 if (h.account_id, h.symbol) == ("schwab", "TSLA")), None)
    return ((held.shares if held is not None else None),
            (held.original_avg if held is not None else None),
            (held.adjusted_avg if held is not None else None),
            sum((r.realized for r in covers), D("0")),
            sum((r.shares_sold for r in covers), D("0")))


@pytest.mark.parametrize(
    ("qty", "price"),
    [
        pytest.param("30", "200", id="partial-cover-still-short"),
        pytest.param("50", "200", id="exact-cover-flat"),
        pytest.param("80", "200", id="over-cover-goes-long"),
    ],
)
def test_buy_cover_preview_matches_every_column_the_ledger_books(
    qty: str, price: str
) -> None:
    """Short 50 open at 260; a buy at 200 covers it. Shares, BOTH averages, realized, cover."""
    conn = _conn()
    _tx(conn, Side.BUY, "100", "240", date(2026, 4, 1))
    _tx(conn, Side.SELL, "150", "260", date(2026, 6, 10), short=True)   # 50 short @ 260
    body = _buy_body(qty, price)
    pp = _preview(conn, body)
    shares, orig_avg, adj_avg, realized, covered = _booked_buy_state(conn, body)
    assert Decimal(str(pp["new_shares"])) == (shares if shares is not None else D("0"))
    for key, booked in (("new_original_avg", orig_avg), ("new_adjusted_avg", adj_avg)):
        shown_raw = pp.get(key)
        shown = None if shown_raw is None else Decimal(str(shown_raw))
        assert shown == booked, f"{key}: preview {shown!r} != booked {booked!r}"
    assert Decimal(str(pp["realized_pnl"])) == realized
    assert Decimal(str(pp["covered_shares"])) == covered
    conn.close()


# --- M4-01 (2026-09-02): the DATE dimension, which every guard above was blind to --------
#
# Everything above replays a hypothetical trade dated AFTER every row in its fixture, so
# "the state the trade sees" and "the state at the end of the ledger" are the same object
# and the identity holds either way. `_position_preview` read the END state — `book.holdings`
# for the (account, symbol), with `body.date` never consulted — so the moment a trade is not
# the last event, every projected column is computed against a position the replay will not
# show it:
#
#   * a BACK-DATED row (補登) sees later buys/sells that had not happened yet, and
#   * a row dated TODAY still sees the SAME DAY's dividends, because `EventPriority` books
#     SELL (30) before DIVIDEND (40) — the user's eye calls that "yesterday", not a backfill.
#
# The 賣超 guard was made date-aware on 2026-07-31 (`holdings.shares_through`); the money
# projections on the same card were not, so the check strip printed 「✓ 可寫入」 above numbers
# that disagreed with the row about to be written.
#
# These cases assert the SAME identity as the guards above — preview == what `build_book`
# actually books — but with the hypothetical row placed in the MIDDLE of the ledger, and over
# EVERY projected column (both averages and the share count, not only the realized amount),
# per domain-ledger.md's 2026-08-27 widening.


def _div(conn: sqlite3.Connection, net: str, d: date) -> None:
    insert_dividend(conn, account_id="schwab", symbol="TSLA", div_date=d,
                    div_type="CASH", gross=D(net), withholding=D("0"), net=D(net))


def _realized_on(conn: sqlite3.Connection, day: date) -> list[RealizedRow]:
    """Realized rows the CURRENT ledger books on *day*, in replay order."""
    return [r for r in build_book(load_ledger_bundle(conn), allow_oversell=True).realized.rows
            if r.sell_date == day]


def _preview_at(
    conn: sqlite3.Connection, body: ManualBody, fee: str = "0", tax: str = "0"
) -> dict[str, object]:
    out = _position_preview(conn, body, D(fee), D(tax), body.shares * body.price)
    assert out is not None
    return out


def _mirror(
    conn: sqlite3.Connection, body: ManualBody, *, fee: str = "0", tax: str = "0"
) -> dict[str, object]:
    """Project, then WRITE the same row, then demand the replay agrees on every column.

    The row is written with the fee/tax the preview was given, so the only variable under
    test is WHERE in the ledger the row lands. Compared against ``build_book`` rather than
    against typed constants for the reason the guards above state: a constant can agree
    with a wrong preview.
    """
    pp = _preview_at(conn, body, fee, tax)
    side = Side.SELL if pp["kind"] == "sell" else Side.BUY
    was = _realized_on(conn, body.date)
    _tx(conn, side, str(body.shares), str(body.price), body.date,
        short=body.short_sale, fee=fee, tax=tax)
    book = build_book(load_ledger_bundle(conn), allow_oversell=True)
    held = next((h for h in book.holdings
                 if (h.account_id, h.symbol) == ("schwab", "TSLA")), None)

    shares_key = "remain_shares" if pp["kind"] == "sell" else "new_shares"
    booked_shares = held.shares if held is not None else D("0")
    assert Decimal(str(pp[shares_key])) == booked_shares, (
        f"{shares_key}: preview {pp[shares_key]!r} != booked {booked_shares}")
    for key, booked_avg in (("new_original_avg", held.original_avg if held else None),
                            ("new_adjusted_avg", held.adjusted_avg if held else None)):
        raw = pp.get(key)
        shown = None if raw is None else Decimal(str(raw))
        assert shown == booked_avg, f"{key}: preview {shown!r} != booked {booked_avg!r}"

    # Attribution: the drafted row books LAST within its own (date, rank) group, and every
    # fixture here is built so no later same-day event books a realized row either — so the
    # rows past the pre-write watermark on that date are exactly this trade's.
    rows = _realized_on(conn, body.date)[len(was):]
    if pp.get("realized_pnl") is None:
        assert rows == [], f"the ledger booked {rows} where the preview showed nothing"
        assert pp.get("cost_removed") is None
    else:
        assert len(rows) == 1, rows
        assert Decimal(str(pp["realized_pnl"])) == rows[0].realized, (
            f"realized: preview {pp['realized_pnl']} != booked {rows[0].realized}")
        if pp["kind"] == "sell":
            assert Decimal(str(pp["cost_removed"])) == rows[0].adjusted_cost_removed
    return pp


def _backdated_ledger(conn: sqlite3.Connection) -> None:
    """The M4-01 repro's shape: one old buy, then a busy day AFTER the trade being drafted."""
    _tx(conn, Side.BUY, "2000", "600", date(2026, 1, 6), fee="171")
    _tx(conn, Side.BUY, "700", "520", date(2026, 9, 1))
    _tx(conn, Side.SELL, "100", "610", date(2026, 9, 1))
    _div(conn, "3000", date(2026, 9, 1))


def _sell_on(day: date, qty: str, price: str, *, short: bool = False) -> ManualBody:
    return ManualBody(account_id="schwab", symbol="TSLA", side="SELL", date=day,
                      shares=D(qty), price=D(price), short_sale=short)


def test_backdated_sell_realizes_against_the_position_that_existed_on_its_own_date() -> None:
    """The reported repro, with the reporter's own arithmetic as the literal.

    On 2026-06-01 the position is the 2026-01-06 buy alone — 2,000 shares,
    ``adjusted_total`` 1,200,171 — so selling 100 removes 1,200,171 x (100/2000) = 60,008.55
    and realizes 69,691 - 60,008.55 = **9,682.45**. The end-state position (2,600 shares,
    later buys folded in) removes only 1/26th of a bigger total and reports 13,881.65 — a
    4,199.20 overstatement on a card whose check strip reads 「可寫入」.
    """
    conn = _conn()
    _backdated_ledger(conn)
    pp = _mirror(conn, _sell_on(date(2026, 6, 1), "100", "700"), fee="99", tax="210")
    assert Decimal(str(pp["cost_removed"])) == D("60008.55")
    assert Decimal(str(pp["realized_pnl"])) == D("9682.45")
    conn.close()


def test_backdated_buy_projects_the_averages_the_replay_will_hold() -> None:
    """A buy 補登 ahead of a later sell: the sell then removes a DIFFERENT fraction.

    Reading the end state instead makes the projected average land on the pre-existing
    position plus this trade's cost — an average the ledger never holds, because the later
    sell was replayed against a book that did not contain this buy.
    """
    conn = _conn()
    _tx(conn, Side.BUY, "2000", "600", date(2026, 1, 6), fee="171")
    _tx(conn, Side.SELL, "500", "650", date(2026, 9, 1))
    body = ManualBody(account_id="schwab", symbol="TSLA", side="BUY",
                      date=date(2026, 6, 15), shares=D("100"), price=D("500"))
    pp = _mirror(conn, body, fee="71")
    assert pp["new_shares"] == "1600"
    conn.close()


def test_a_same_day_dividend_is_replayed_AFTER_the_sell_and_the_preview_must_agree() -> None:
    """Not a backfill at all — the user calls this date 「昨天」.

    ``EventPriority`` books SELL (30) before DIVIDEND (40), so a sell dated today sees the
    cost basis BEFORE that day's payouts reduce it. The end-state read saw the reduced
    ``adjusted_total`` and understated the cost removed (and so overstated the gain).
    """
    conn = _conn()
    _tx(conn, Side.BUY, "100", "30", date(2026, 1, 5))
    _div(conn, "50", date(2026, 9, 1))
    _div(conn, "20", date(2026, 9, 1))
    pp = _mirror(conn, _sell_on(date(2026, 9, 1), "10", "40"))
    # adjusted_total is still 3,000 when the sell books: 3,000 x (10/100) = 300.
    assert Decimal(str(pp["cost_removed"])) == D("300")
    assert Decimal(str(pp["realized_pnl"])) == D("100")
    conn.close()


def test_a_sell_dated_before_the_position_existed_is_the_oversell_it_will_be_booked_as() -> None:
    """The money card must reach the same verdict the date-aware 賣超 guard already reaches.

    Nothing is held on 2026-04-01; the covering buy is a month later. The replay takes the
    undeclared-oversell branch — basis discarded, no realized row — while the end-state read
    saw 100 shares and projected an ordinary sale.
    """
    conn = _conn()
    _tx(conn, Side.BUY, "100", "240", date(2026, 5, 1))
    body = _sell_on(date(2026, 4, 1), "50", "300")
    pp = _preview_at(conn, body)
    assert pp["oversell"] is True, "an end-state read calls this an ordinary sale"
    assert pp["realized_pnl"] is None and pp["cost_removed"] is None
    _mirror(conn, body)
    conn.close()


@pytest.mark.parametrize(
    ("day", "qty", "price", "short", "fee", "tax"),
    [
        pytest.param(date(2026, 6, 1), "100", "700", False, "99", "210", id="backdated-sell"),
        pytest.param(date(2026, 6, 1), "3000", "700", False, "0", "0",
                     id="backdated-oversell"),
        pytest.param(date(2026, 6, 1), "3000", "700", True, "0", "0",
                     id="backdated-declared-partial"),
        pytest.param(date(2026, 6, 1), "2000", "700", False, "0", "0",
                     id="backdated-full-exit-then-rebought"),
        pytest.param(date(2026, 9, 1), "100", "610", False, "0", "0",
                     id="same-day-before-the-dividend"),
        pytest.param(date(2026, 9, 2), "100", "610", False, "0", "0",
                     id="after-everything-unchanged"),
    ],
)
def test_every_projected_column_mirrors_the_replay_wherever_the_row_lands(
    day: date, qty: str, price: str, short: bool, fee: str, tax: str
) -> None:
    """One ledger, six insertion points, every projected column — the guard with a date axis.

    ``after-everything-unchanged`` is the counter-evidence: the happy path was never broken,
    so a fix that "corrects" it would be fixing the wrong thing.
    """
    conn = _conn()
    _backdated_ledger(conn)
    _mirror(conn, _sell_on(day, qty, price, short=short), fee=fee, tax=tax)
    conn.close()
