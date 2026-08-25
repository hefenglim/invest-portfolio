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
    insert_transaction,
    load_ledger_bundle,
    upsert_instrument,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

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
        short: bool = False) -> None:
    insert_transaction(conn, account_id="schwab", symbol="TSLA", side=side,
                       quantity=D(qty), price=D(price), fees=D("0"), tax=D("0"),
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
