"""The SPINOFF child's SEED price — its one writer, its signature, and its conditional removal.

A SPINOFF creates a holding that did not exist to be quoted, and ``returns.py`` is all-or-
nothing on the XIRR terminal value, so the corporate-action form lets the owner type the
child's opening price off the statement (D48b). That row is the only price in the table a
human typed rather than a provider delivered, and this module is its only owner (DEF-040,
owner ruling 2026-09-24): ``pricing/`` owns every write to ``prices`` (``architecture.md``),
so both the write and the removal live here, and the ``api`` layer — which knows which
corporate action is being saved or deleted — binds them.

**The signature, and why it needs no record.** A seed is stored with ``source =``
:data:`SEED_SOURCE` and ``fetched_at`` = the ACTION DAY at 00:00 (QA-05: the owner dated the
price themselves, so it was "observed" on its own date). No provider write can produce that
pair — providers stamp their own source and the wall-clock fetch time — and a later quote for
the same ``(instrument, as_of_date)`` replaces BOTH through ``upsert_prices``' ``ON CONFLICT``.
So "the row the action wrote, not since overwritten by a real quote" is decidable from the row
itself, which also covers every seed written before this module existed.

**Conditional reversal — the DEF-021 / F-3 rule for bands and weights, applied to a price.**
Deleting the action removes the seed ONLY while the signature is intact; a row that is now a
provider's quote is the market's number and stays, and the verdict says why — the same
``restorable`` / ``restored`` / ``reason`` shape the delete confirm and toast already read for
the band and the weight.

**A seed is written ONLY into an EMPTY slot (DEF-040 R4, the verifier's R3 bounce).** R3 wrote
through ``upsert_prices``' ``ON CONFLICT``, so a provider quote already on the action day was
REPLACED with no copy kept — and since the replacement then carried the seed signature, the
delete removed it too: the quote was gone for good while the list promised ``restorable`` and
the delete answered ``restored``. :func:`write_seed_price` now refuses any occupied slot — a
provider's quote, or another seed (an orphan, or another save's) — and returns the refusal
(:class:`SeedSkip`) for the form and the toast to say. A signature alone also cannot tell THIS
action's seed from an orphan with the same stamp, so the binder records what each save wrote
(``corporate_actions.child_seed_json``) and passes the typed close back here as
``expected_close``: the delete takes back exactly that row, or nothing.
"""

import sqlite3
from datetime import date, datetime, time, tzinfo
from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Market

#: The ``prices.source`` of a SPINOFF child's seed. RESERVED for it: a second writer using this
#: tag would make its rows removable by a corporate-action delete. (The literal predates this
#: module — seeds written since D48b carry it — which is why it is not a more specific word.)
SEED_SOURCE = "manual"


class SeedPriceVerdict(BaseModel):
    """What deleting a SPINOFF does to the child's seed price (DEF-040).

    ``removable`` is the PREDICATE the delete confirm quotes (:func:`pending_seed_removal`);
    ``removed`` is what the delete did (:func:`remove_seed_price`); ``reason`` is the zh
    sentence shown when the price stays."""

    symbol: str
    as_of: date
    removable: bool
    removed: bool = False
    reason: str | None = None
    #: The seed's typed close (``close_raw``) when the row IS a removable seed — what an
    #: edit that moves the seed to another day or child re-writes there (DEF-060).
    close: Decimal | None = None


def seed_stamp(on: date, tz: tzinfo | None) -> datetime:
    """The ``fetched_at`` a seed dated *on* is written with: that day at 00:00, in the
    request clock's timezone (QA-05 — only the DATE moves, the tz convention is the column's)."""
    return datetime.combine(on, time.min, tzinfo=tz)


class SeedSkip(BaseModel):
    """Why a seed was NOT written (DEF-040 R4): the ``(symbol, as_of)`` slot already holds a
    row, and a seed never overwrites one. ``existing_close`` is the stored TEXT verbatim;
    ``reason`` is the zh sentence the form and the toast show."""

    symbol: str
    as_of: date
    existing_close: str
    source: str
    reason: str


class SeedWrite(BaseModel):
    """What :func:`write_seed_price` did: ``written``, or the :class:`SeedSkip` it refused on."""

    written: bool
    skipped: SeedSkip | None = None


def occupied_slot(conn: sqlite3.Connection, *, symbol: str, on: date) -> SeedSkip | None:
    """The refusal a seed for *symbol* on *on* would meet, or ``None`` when the slot is free.

    The ONE predicate behind the form's notice (read on every preview), the save and an
    edit's move, so the three cannot disagree. ANY row counts: a provider's quote is the
    market's number, and a seed-signature row is either the same event's (the ledger refuses
    a second action touching one symbol on one day) or an orphan whose owner is gone —
    overwriting either would let a later delete remove a row that was there before this
    save. Degrades to ``None`` when the ``prices`` table does not exist (the write then
    raises and the caller degrades).
    """
    try:
        row = conn.execute(
            "SELECT close, source FROM prices WHERE instrument=? AND as_of_date=?",
            (symbol, on.isoformat()),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    close, source = str(row[0]), str(row[1])
    when = on.isoformat()
    reason = (
        f"{symbol} 在 {when} 已有一筆手動輸入的起始價 {close}，起始價未寫入（既有價格不覆蓋）"
        if source == SEED_SOURCE
        else f"{symbol} 在 {when} 已有正式報價 {close}（來源 {source}），起始價未寫入"
    )
    return SeedSkip(symbol=symbol, as_of=on, existing_close=close, source=source,
                    reason=reason)


def write_seed_price(
    conn: sqlite3.Connection, *, symbol: str, market: Market, on: date, close: Decimal,
    tz: tzinfo | None,
) -> SeedWrite:
    """Store the child's seed through the ONE price write seam, stamped with the signature —
    ONLY into an empty slot (DEF-040 R4); an occupied one is left untouched and the refusal
    is returned instead.

    ``upsert_prices`` is called with its identity ``factor_of`` on purpose: with ``fetched_at``
    on the action day the window ``(as_of, fetched_at]`` is empty, so no split can apply to
    this row now or on any later reconcile (see ``api/routers/ledgers.py::_seed_child_price``
    for the measured back-dated case). Raises ``sqlite3.OperationalError`` on a ledger-only
    database with no ``prices`` table; the caller degrades.
    """
    if (skip := occupied_slot(conn, symbol=symbol, on=on)) is not None:
        return SeedWrite(written=False, skipped=skip)
    upsert_prices(
        conn,
        [PriceRow(instrument=symbol, market=market, as_of=on, close=close,
                  source=SEED_SOURCE)],
        fetched_at=seed_stamp(on, tz),
    )
    return SeedWrite(written=True)


def has_seed_signature(source: str, fetched_at: str, on: date) -> bool:
    """Whether a ``prices`` row dated *on* carries the seed SIGNATURE: ``source`` =
    :data:`SEED_SOURCE` and ``fetched_at`` = *on* at 00:00. It says "a seed", never whose —
    ownership is ``data_ingestion.store.StoredCorporateAction.owned_seed_slot``'s question
    (``pricing/`` knows nothing about corporate actions, D17). Public since DEF-063: the
    orphan-seed cleanup script applies the same signature test the delete does."""
    if source != SEED_SOURCE:
        return False
    try:
        stamp = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    return stamp.date() == on and stamp.time() == time.min


def pending_seed_removal(
    conn: sqlite3.Connection, *, symbol: str, on: date, still_used: bool = False,
    expected_close: Decimal | None = None,
) -> SeedPriceVerdict | None:
    """What deleting the SPINOFF that created *symbol* on *on* WOULD do to its price row,
    without doing it — the delete confirm's read, and the predicate the delete then runs.

    ``None`` when there is no price row for that day at all (nothing was seeded, nothing is
    there to keep): the confirm has nothing to say. ``still_used`` is the binder's answer to
    "does another corporate action that stays in the ledger own this same seed?" — then the
    seed still belongs to that one and stays. ``expected_close`` is the close the save
    RECORDED writing (DEF-040 R4): a seed-signature row holding a different value is not the
    one this action wrote, and stays. ``None`` = no record (a row saved before the record
    existed): the signature alone decides — R3's rule.

    Degrades to ``None`` when the ``prices`` table does not exist (``bootstrap_db`` does not
    create it — the same condition ``validate._has_prices`` absorbs).
    """
    try:
        row = conn.execute(
            "SELECT source, fetched_at, close_raw, close FROM prices "
            "WHERE instrument=? AND as_of_date=?",
            (symbol, on.isoformat()),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    source, fetched_at = str(row[0]), str(row[1])
    if not has_seed_signature(source, fetched_at, on):
        return SeedPriceVerdict(symbol=symbol, as_of=on, removable=False, reason=(
            f"子公司 {symbol} 在 {on.isoformat()} 的價格目前是正式報價（來源 {source}）"
            "— 登錄時的起始價已被覆蓋或未曾填寫，這筆報價保留不刪"))
    stored = str(row[2] if row[2] is not None else row[3])
    if expected_close is not None and Decimal(stored) != expected_close:
        return SeedPriceVerdict(symbol=symbol, as_of=on, removable=False, reason=(
            f"子公司 {symbol} 在 {on.isoformat()} 的起始價目前是 {stored}，"
            f"不是這筆分拆登錄時寫入的 {expected_close}，保留不刪"))
    if still_used:
        return SeedPriceVerdict(symbol=symbol, as_of=on, removable=False, reason=(
            f"仍有其他分拆紀錄在 {on.isoformat()} 建立 {symbol}，登錄時寫入的起始價保留"))
    return SeedPriceVerdict(symbol=symbol, as_of=on, removable=True, close=Decimal(stored))


def remove_seed_price(
    conn: sqlite3.Connection, *, symbol: str, on: date, still_used: bool = False,
    expected_close: Decimal | None = None, commit: bool = True,
) -> SeedPriceVerdict | None:
    """Delete the seed when :func:`pending_seed_removal` says it is safe — the conditional
    inverse of :func:`write_seed_price`. ``commit=False`` hands the transaction to the caller
    (the corporate-action delete removes its rows, fee, band, weight and this price as one)."""
    verdict = pending_seed_removal(conn, symbol=symbol, on=on, still_used=still_used,
                                   expected_close=expected_close)
    if verdict is None or not verdict.removable:
        return verdict
    conn.execute(
        "DELETE FROM prices WHERE instrument=? AND as_of_date=? AND source=?",
        (symbol, on.isoformat(), SEED_SOURCE),
    )
    if commit:
        conn.commit()
    return verdict.model_copy(update={"removed": True})
