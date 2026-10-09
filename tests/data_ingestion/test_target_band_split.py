"""D44 — the owner's target band, and the SPLIT that silently invalidates it.

W6c re-expresses the price ``target_cross`` compares; the band is owner-entered and is NOT
re-expressed (§5.1(d2)), so after a 7-for-1 a stale band of 200 meets a price of ~28.6 and
the rule crosses **immediately and permanently** — on the notification path. The owner's
ruling (2026-08-15) is option (b): do not guess, **ask once at entry**, and show the
restated number so the answer is one click rather than a trip to another page.

Every "it fires" below is paired with an "it stays quiet", because the expensive failure is
not a missed warning — it is a warning that fires on rows it should not, which is what
``target_set_at`` exists to prevent and what ``test_a_HISTORICAL_split_is_silent`` pins.
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.corporate_action_import import (
    build_corporate_action_preview,
)
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.data_ingestion.validate import (
    TARGET_BAND_PREDATES_SPLIT,
    CorporateActionInput,
    Issue,
    restated_band,
    validate_corporate_action,
)
from portfolio_dash.shared.corporate_actions import apply_ratio_to_price
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

D = Decimal
BUY_DAY = date(2026, 1, 10)
ACTION_DAY = date(2026, 6, 15)
BEFORE = date(2026, 3, 1)   # a band set BEFORE the split — the case the finding is for
AFTER = date(2026, 7, 1)    # a band set AFTER it — already in post-split terms


def _soft(issues: list[Issue]) -> set[str]:
    return {i.kind for i in issues if i.needs_confirm}


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    c.executemany(
        "INSERT INTO accounts (account_id, name, broker, settlement_ccy, funding_ccy, "
        "fee_rule_set, dividend_model) VALUES (?,?,?,?,?,?,?)",
        [
            ("schwab", "Schwab", "Schwab", "USD", "TWD", "schwab", "drip_us"),
            ("moomoo_my", "Moomoo", "Moomoo MY", "USD", "MYR", "moomoo_us", "drip_us"),
        ],
    )
    for sym in ("AAA", "BBB"):
        upsert_instrument(c, Instrument(symbol=sym, market=Market.US,
                                        quote_ccy=Currency.USD, sector="Tech", name=sym))
    # AAA held in BOTH accounts before the action — E1a needs a position, E13 needs both.
    for acct in ("schwab", "moomoo_my"):
        insert_transaction(c, account_id=acct, symbol="AAA", side=Side.BUY,
                           quantity=D("700"), price=D("50"), fees=D("0"), tax=D("0"),
                           trade_date=BUY_DAY)
    c.commit()
    return c


def _band(
    conn: sqlite3.Connection, symbol: str, *, low: str | None = None,
    high: str | None = None, on: date,
) -> None:
    """Set *symbol*'s band as if the owner had entered it on *on*."""
    from portfolio_dash.data_ingestion.store import get_instrument

    inst = get_instrument(conn, symbol)
    assert inst is not None
    upsert_instrument(
        conn,
        inst.model_copy(update={"target_low": D(low) if low else None,
                                "target_high": D(high) if high else None}),
        today=on,
    )


def _inp(**over: object) -> CorporateActionInput:
    base: dict[str, object] = {
        "account_id": "schwab", "date": ACTION_DAY, "kind": "SPLIT",
        "from_symbol": "AAA", "to_symbol": "AAA",
        "ratio_to": D("7"), "ratio_from": D("1"),
    }
    base.update(over)
    return CorporateActionInput(**base)  # type: ignore[arg-type]


def _both(**over: object) -> list[CorporateActionInput]:
    return [_inp(account_id="schwab", **over), _inp(account_id="moomoo_my", **over)]


# --------------------------------------------------------------------------- it fires


def test_a_band_set_before_the_split_is_flagged_with_the_restated_number(
    conn: sqlite3.Connection,
) -> None:
    """The whole case, end to end: the finding fires, it is SOFT (acknowledgeable, not a
    rejection — the split is real and must be recordable), and it quotes the number the
    owner would have to work out otherwise."""
    _band(conn, "AAA", low="200", on=BEFORE)
    batch = _both()
    issues = validate_corporate_action(conn, batch[0], batch=batch)

    assert TARGET_BAND_PREDATES_SPLIT in _soft(issues)
    (found,) = [i for i in issues if i.kind == TARGET_BAND_PREDATES_SPLIT]
    assert "28.5714" in found.message            # 200 × 1/7, the restated floor
    assert BEFORE.isoformat() in found.message   # …and WHEN the stale band was set
    assert "系統不會替你決定" in found.message    # the ruling itself, in the message


def test_both_legs_are_named_when_both_are_set(conn: sqlite3.Connection) -> None:
    _band(conn, "AAA", low="200", high="280", on=BEFORE)
    batch = _both()
    (found,) = [i for i in validate_corporate_action(conn, batch[0], batch=batch)
                if i.kind == TARGET_BAND_PREDATES_SPLIT]
    assert "28.5714" in found.message and "40" in found.message


def test_a_reverse_split_is_flagged_too(conn: sqlite3.Connection) -> None:
    """1-for-20 pushes the band the other way (2 → 40). Nothing about the finding is
    direction-specific, and a reverse split breaks the comparison just as completely."""
    _band(conn, "AAA", low="2", on=BEFORE)
    batch = _both(ratio_to=D("1"), ratio_from=D("20"))
    (found,) = [i for i in validate_corporate_action(conn, batch[0], batch=batch)
                if i.kind == TARGET_BAND_PREDATES_SPLIT]
    assert "40" in found.message


# ------------------------------------------------------------------------ it stays quiet


def test_a_HISTORICAL_split_is_silent(conn: sqlite3.Connection) -> None:
    """★ The discriminator, and the reason ``target_set_at`` exists at all.

    Importing years of broker history means importing OLD splits against bands set
    recently — the band is already in post-split terms and there is nothing to say. Without
    this term the one-click broker import would raise a false warning on nearly every
    historical split it carries, and a guard that mostly cries wolf trains the owner to
    click through the one time it is right (E23's own argument for its fourth term).
    """
    _band(conn, "AAA", low="200", on=AFTER)
    batch = _both()
    assert TARGET_BAND_PREDATES_SPLIT not in _soft(
        validate_corporate_action(conn, batch[0], batch=batch))


def test_an_undated_band_makes_no_claim(conn: sqlite3.Connection) -> None:
    """A row that migrated in before the column existed: the band is real, its date is
    unknowable. Silence is the honest answer — the same degradation ``covered_ratio`` and
    ``_has_prices`` already use. Guessing "probably old" would put the false positive back."""
    _band(conn, "AAA", low="200", on=BEFORE)
    conn.execute("UPDATE instruments SET target_set_at=NULL WHERE symbol='AAA'")
    conn.commit()
    batch = _both()
    assert TARGET_BAND_PREDATES_SPLIT not in _soft(
        validate_corporate_action(conn, batch[0], batch=batch))


def test_no_band_no_finding(conn: sqlite3.Connection) -> None:
    batch = _both()
    assert TARGET_BAND_PREDATES_SPLIT not in _soft(
        validate_corporate_action(conn, batch[0], batch=batch))


def test_a_one_for_one_split_is_silent(conn: sqlite3.Connection) -> None:
    """It moves no share and no price, so it invalidates no band. E7 already asks whether
    the owner meant to record it at all; a second question about the band would be noise."""
    _band(conn, "AAA", low="200", on=BEFORE)
    batch = _both(ratio_to=D("1"), ratio_from=D("1"))
    assert TARGET_BAND_PREDATES_SPLIT not in _soft(
        validate_corporate_action(conn, batch[0], batch=batch))


def test_exchange_and_spinoff_are_silent(conn: sqlite3.Connection) -> None:
    """§5.1's price re-expression is SPLIT-scoped, so only a SPLIT puts the two sides of
    the comparison into different denominations. An EXCHANGE strands the band on a symbol
    the owner no longer holds — a different problem, answered by D47, not by this warning."""
    _band(conn, "AAA", low="200", on=BEFORE)
    for over in ({"kind": "EXCHANGE", "to_symbol": "BBB"},
                 {"kind": "SPINOFF", "to_symbol": "BBB", "cost_carry": D("0.3")}):
        batch = _both(**over)
        assert TARGET_BAND_PREDATES_SPLIT not in _soft(
            validate_corporate_action(conn, batch[0], batch=batch)), over


# ----------------------------------------------------------------- the doors, and the math


def test_the_CSV_door_raises_it_too(conn: sqlite3.Connection) -> None:
    """The bulk door must not ship a weaker guard than the single-row form — the asymmetry
    ``architecture.md`` names for the cash guard. Raised in the validator, so this comes for
    free; asserted anyway, because "for free" is exactly what stops being true silently.
    """
    _band(conn, "AAA", low="200", on=BEFORE)
    csv_text = (
        "account,date,kind,from_symbol,to_symbol,ratio_to,ratio_from,cost_carry,note\n"
        f"schwab,{ACTION_DAY.isoformat()},SPLIT,AAA,AAA,7,1,,\n"
        f"moomoo_my,{ACTION_DAY.isoformat()},SPLIT,AAA,AAA,7,1,,\n"
    )
    preview = build_corporate_action_preview(conn, csv_text)
    kinds = {i.kind for row in preview.rows for i in row.issues}
    assert TARGET_BAND_PREDATES_SPLIT in kinds
    # …and SOFT, so the rows still commit once acknowledged. A hard issue here would make
    # a real split unrecordable because of an alert setting, which inverts the priorities.
    assert not any(r.has_hard_issue for r in preview.rows)


def test_every_account_row_carries_it_so_no_door_can_lose_it(
    conn: sqlite3.Connection,
) -> None:
    """One event, N rows (D13). The band is per-SYMBOL, so the finding is identical on each
    — deliberately, because each row must be individually acknowledgeable in a CSV preview.
    The form's flat issue list collapses the copies in ``ledgers._issue_wires`` (keyed on
    code+text), which is why the message names no account."""
    _band(conn, "AAA", low="200", on=BEFORE)
    batch = _both()
    for row in batch:
        assert TARGET_BAND_PREDATES_SPLIT in _soft(
            validate_corporate_action(conn, row, batch=batch))


def test_restated_band_returns_the_set_legs_only(conn: sqlite3.Connection) -> None:
    inst = Instrument(symbol="AAA", market=Market.US, quote_ccy=Currency.USD,
                      sector="Tech", name="AAA", target_high=D("280"))
    assert [(f, cur, new) for f, _lbl, cur, new in
            restated_band(inst, ratio_to=D("7"), ratio_from=D("1"))] == [
        ("target_high", D("280"), D("40")),
    ]
    bare = inst.model_copy(update={"target_high": None})
    assert restated_band(bare, ratio_to=D("7"), ratio_from=D("1")) == []


def test_the_restatement_is_the_price_direction_and_rounds_once() -> None:
    """``value × from / to`` — the reciprocal of the share direction, so the two multiply
    out to an unchanged position value. It quantizes (unlike ``apply_ratio``) because the
    result is a QUOTIENT: 200 × 1/7 does not terminate, and the number in the message, the
    number in the payload and the number written must be one number."""
    assert apply_ratio_to_price(D("200"), ratio_to=D("7"), ratio_from=D("1")) == D("28.5714")
    assert apply_ratio_to_price(D("2"), ratio_to=D("1"), ratio_from=D("20")) == D("40")
    # Exact where it can be exact: a 2-for-1 halves cleanly, with no trailing noise.
    assert apply_ratio_to_price(D("50"), ratio_to=D("2"), ratio_from=D("1")) == D("25")
