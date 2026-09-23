"""Per-symbol target-weight config (Blueprint P3 batch 2, owner ruling D8 2026-07-12).

A single-row JSON config (``target_weights_config``) mapping a REGISTERED symbol to its
target reporting-currency weight as a **Decimal-string RATIO** (``"0.25"`` = 25%). This is
the SINGLE source of truth for two consumers: the ``rebalance_drift`` alert rule
(strategy/alerts) and the rebalance-preview drawer's default prefill (both are FED the
same stored ratios — never a second copy).

Weights are ratios, not money, so the 2-dp money rule never applies — they are stored at
4-dp ratio precision (``data-and-pricing.md``). An absent / empty map means "no targets
set"; a symbol absent from the map is simply un-targeted (the drift rule stays silent for
it). Validation (each weight ∈ (0,1], Σ ≤ 1, symbol is registered) lives at the API write
seam — this store is pure persistence and imports only ``shared.config_store`` plus the
EXCHANGE record's row shape from ``data_ingestion.store`` (``MovedWeight``, F-3 — an edge
``architecture.md`` already allows ``strategy``; no pricing / llm_insight), mirroring
``strategy.rules_config``.
"""

import json
import sqlite3
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.data_ingestion.store import MovedWeight, get_instrument
from portfolio_dash.shared import config_store

_CATEGORY = "target_weights"
_DDL = (
    "CREATE TABLE IF NOT EXISTS target_weights_config "
    "(id INTEGER PRIMARY KEY CHECK (id = 1), weights_json TEXT NOT NULL, updated_at TEXT)"
)


def _create(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO target_weights_config (id, weights_json, updated_at) "
        "VALUES (1, '{}', NULL)"
    )


def ensure_target_weights_seeded(conn: sqlite3.Connection) -> None:
    """Create the single-row target-weights table (always) and seed it empty (once)."""
    config_store.ensure_seeded(conn, _CATEGORY, create=_create, seed=_seed)


def load_target_weights(conn: sqlite3.Connection) -> dict[str, Decimal]:
    """Read the stored target weights as ``{symbol: Decimal ratio}`` ({} when unset).

    Degrades to ``{}`` when the table is absent (a fresh DB before bootstrap seeds it) so a
    dashboard/alerts read never crashes — matches the ``account_display_names`` discipline.
    """
    try:
        row = conn.execute(
            "SELECT weights_json FROM target_weights_config WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if row is None or row[0] is None:
        return {}
    raw = json.loads(row[0])
    return {str(sym): Decimal(str(val)) for sym, val in raw.items()}


def save_target_weights(
    conn: sqlite3.Connection, weights: dict[str, Decimal], *, now: datetime,
    commit: bool = True,
) -> None:
    """Persist *weights* (single-row upsert). Values stored as canonical Decimal strings.

    The caller has already validated (registered symbols, each ∈ (0,1], Σ ≤ 1); this only
    writes. An empty map is a valid state (clears all targets).

    ``commit=False`` (F-3) defers to a caller that owns the transaction — the corporate-action
    save and delete, so the weight lands with the action rows or not at all. That path skips
    the seed helper on purpose: ``config_store.ensure_seeded`` COMMITS on a first seed, which
    would commit the caller's half-written rows mid-transaction. It only needs the table
    (``CREATE … IF NOT EXISTS`` never commits), and it is only reached with a weight already
    read from that table, so the row exists.
    """
    if commit:
        ensure_target_weights_seeded(conn)
    else:
        _create(conn)
    payload = {sym: str(w) for sym, w in weights.items()}
    conn.execute(
        "INSERT INTO target_weights_config (id, weights_json, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET weights_json = excluded.weights_json, "
        "updated_at = excluded.updated_at",
        (json.dumps(payload), now.isoformat()),
    )
    if commit:
        conn.commit()


def pending_weight_move(
    conn: sqlite3.Connection, *, from_symbol: str, to_symbol: str
) -> MovedWeight | None:
    """What :func:`move_target_weight` WOULD move, without moving it (F-3).

    The one predicate: the corporate-action save records this on the EXCHANGE row BEFORE the
    move, and the move re-asks it, so the record and the write cannot disagree — the same
    reason ``store.pending_band_move`` exists beside ``move_target_band``.
    """
    if from_symbol == to_symbol:
        return None
    weights = load_target_weights(conn)
    moved = weights.get(from_symbol)
    if moved is None or to_symbol in weights:
        return None
    return MovedWeight(from_symbol=from_symbol, to_symbol=to_symbol, weight=moved)


def move_target_weight(
    conn: sqlite3.Connection, *, from_symbol: str, to_symbol: str, now: datetime,
    commit: bool = True,
) -> Decimal | None:
    """Re-key one symbol's target weight after an EXCHANGE. Returns the weight, or ``None``.

    The map is keyed by SYMBOL STRING, and until 2026-08-16 nothing re-keyed it: a merger or
    a ticker rename left the target stranded on a symbol the ledger no longer holds, where
    it could never be met and instead surfaced as a permanent
    ``rebalance.excluded_with_target`` entry. The same shape as D47's price-alert band, and
    it was missed because a weight is config rather than money — nothing recomputes it, so
    nothing disagreed and no test could notice.

    Four rules, each with a reason:

    * **Move, not copy.** A copy leaves the dead ticker targeted, which is the defect.
    * **The value does not change.** 「25% of the portfolio in this company」 survives a
      change of ticker. (A SPLIT is immune either way — a ratio is unitless, so 25% stays
      25% — which is why only the ticker-changing kinds need this at all.)
    * **A destination the owner already targeted is never overwritten** (D47 parity): that
      number is their judgement about the destination security and no merge rule could be
      right. ⚠ The source's weight then STAYS, and stays stranded — visible in
      ``excluded_with_target``, which is the honest place for it rather than a silent delete
      of a value the owner typed.
    * **SPINOFF does nothing.** The child is a different company; splitting the parent's
      target between them needs an allocation rule nobody has given, and inventing one puts
      a weight the owner never chose into the rebalance engine. The parent keeps its target
      and the child has none.

    Not called from ``data_ingestion``: that would be a new upward edge
    (``architecture.md``). Both corporate-action doors invoke it from ``api/``.

    ⚠ **Inside the action's transaction, and conditionally reversible (F-3, 2026-09-23 —
    DEF-020 / DEF-021's class, found on the weight after both were fixed for the band).** The
    form's save used to call this AFTER its commit, so a failure here left an EXCHANGE whose
    weight never followed it; and a delete never moved the weight back. Now the save passes
    ``commit=False`` inside its own try, records :func:`pending_weight_move` on the row
    (``corporate_actions.weight_move_json``), and the delete runs
    :func:`restore_target_weight` in the delete's transaction.
    """
    moving = pending_weight_move(conn, from_symbol=from_symbol, to_symbol=to_symbol)
    if moving is None:
        return None
    weights = load_target_weights(conn)
    del weights[from_symbol]
    weights[to_symbol] = moving.weight
    save_target_weights(conn, weights, now=now, commit=commit)
    return moving.weight


class WeightRestoreVerdict(BaseModel):
    """Whether deleting an EXCHANGE moves its target weight back (F-3), and why not.

    ``restorable`` is the PREDICATE (read-only, :func:`pending_weight_restore`); ``restored``
    is set by :func:`restore_target_weight` once the write has happened; ``reason`` is the zh
    sentence the delete response carries when the weight stays where it is. The band's
    ``store.BandRestoreVerdict``, for the other setting."""

    weight: MovedWeight
    restorable: bool
    restored: bool = False
    reason: str | None = None


def _pct(weight: Decimal) -> str:
    """「25%」 for a message — a ratio shown the way the 目標配置 panel asks for it."""
    text = format(weight * 100, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text}%"


def pending_weight_restore(
    conn: sqlite3.Connection, moved: MovedWeight | None
) -> WeightRestoreVerdict | None:
    """What deleting the EXCHANGE that recorded *moved* WOULD do to the weight, without doing
    it. ``None`` when nothing was recorded (every non-EXCHANGE row, every EXCHANGE that found
    no weight to move, and every row written before F-3).

    The move is reversed ONLY when both ends are still exactly as the move left them — the
    band's rule (``store.pending_band_restore``), for the same reasons:

    * the destination carries **byte-for-byte** the recorded weight — a weight the owner has
      since changed or cleared is their judgement about the destination and outranks the
      inherited one;
    * the source carries **no** weight — one set on the retired symbol after the move is,
      again, the owner's own, and no merge rule could be right;
    * the source is still a registered symbol — the 目標配置 write seam only accepts
      registered symbols, and a restore must not plant a weight that door would refuse.
    """
    if moved is None:
        return None
    weights = load_target_weights(conn)
    current = weights.get(moved.to_symbol)
    if current is None:
        return WeightRestoreVerdict(weight=moved, restorable=False, reason=(
            f"{moved.to_symbol} 的目標權重在換股後已清除，未自動移回 {moved.from_symbol}"))
    if str(current) != str(moved.weight):
        return WeightRestoreVerdict(weight=moved, restorable=False, reason=(
            f"{moved.to_symbol} 的目標權重在換股後已改動（登錄時 {_pct(moved.weight)}，"
            f"目前 {_pct(current)}），未自動移回 {moved.from_symbol}，"
            "請到「設定 › 目標配置」自行調整"))
    own = weights.get(moved.from_symbol)
    if own is not None:
        return WeightRestoreVerdict(weight=moved, restorable=False, reason=(
            f"{moved.from_symbol} 目前已另有目標權重（{_pct(own)}），未自動移回，"
            f"{moved.to_symbol} 的目標權重保持不變"))
    if get_instrument(conn, moved.from_symbol) is None:
        return WeightRestoreVerdict(weight=moved, restorable=False, reason=(
            f"{moved.from_symbol} 已不在標的清單，目標權重未自動移回"))
    return WeightRestoreVerdict(weight=moved, restorable=True)


def restore_target_weight(
    conn: sqlite3.Connection, moved: MovedWeight | None, *, now: datetime,
    commit: bool = True,
) -> WeightRestoreVerdict | None:
    """F-3: move the weight an EXCHANGE carried across BACK to the retired symbol, when
    :func:`pending_weight_restore` says it is safe — the conditional inverse of
    :func:`move_target_weight`, run by the delete inside the delete's own transaction
    (``commit=False``). Re-keys only: the value is the recorded one, unchanged."""
    verdict = pending_weight_restore(conn, moved)
    if verdict is None or not verdict.restorable:
        return verdict
    weights = load_target_weights(conn)
    del weights[verdict.weight.to_symbol]
    weights[verdict.weight.from_symbol] = verdict.weight.weight
    save_target_weights(conn, weights, now=now, commit=commit)
    return verdict.model_copy(update={"restored": True})


def get_updated_at(conn: sqlite3.Connection) -> str | None:
    """ISO timestamp of the last save, or None when never saved / table absent."""
    try:
        row = conn.execute(
            "SELECT updated_at FROM target_weights_config WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row[0] is None:
        return None
    return str(row[0])
