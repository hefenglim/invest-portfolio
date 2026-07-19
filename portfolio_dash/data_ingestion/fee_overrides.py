"""User-adjustable fee-rule overlay (FU-D1): a DB layer over ``config_seed.FEE_RULES`` v2.

The authoritative fee schedules live in ``config_seed.py::FEE_RULES`` (fee-engine v2, from the
owner's broker doc). This module lets the owner ADJUST individual rate/amount fields at
runtime via a small overlay table WITHOUT touching config: the **effective** rule set =
v2 defaults ⊕ the DB overlay.

Invariants:
  * History is never recomputed. Every transaction row already stores its own
    ``fee_rule_snapshot`` (the regime it was booked under); edits here affect FUTURE fee/tax
    computations only. The snapshot remains the arbiter (``domain-ledger.md`` / §3).
  * Money is ALWAYS ``Decimal``; overlay values are stored as canonical Decimal STRINGS
    (``format(d, "f")``), one field per key, only OVERRIDDEN fields present.
  * ``config_seed.get_fee_rule_set(name, conn=None)`` with ``conn=None`` returns pure v2
    defaults (keeps the oracle + unit tests deterministic); with a ``conn`` it merges the
    overlay — resolved conn-aware at EVERY money call site (the "engine supports it but the
    entry never passes it" bug class, LESSONS_LEARNED.md).
  * Only WHITELISTED fields are editable (the ``Decimal`` fields + ``rounding``); ``market``
    and the computed properties are never editable.

Table (created lazily on the first WRITE — see :func:`ensure_tables`; the read paths degrade
gracefully when it is absent, so a fee resolution NEVER creates a table as a side effect)::

    fee_rule_overrides(rule_set TEXT PRIMARY KEY, overrides TEXT NOT NULL, updated_at TEXT)
"""

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple

from portfolio_dash.data_ingestion.config_seed import FeeRuleSet

# --- Editable-field whitelist, classified for bounds-checking ------------------------------
# Rates are fractions of a notional/fee and must lie in [0, 1]; amounts / per-share values
# must be >= 0; caps are the four nullable ``Decimal | None`` fields (null = no cap) and must
# be >= 0 when present; ``rounding`` is the quantization-mode enum.
_RATE_FIELDS: frozenset[str] = frozenset({
    "brokerage", "discount", "tax_normal", "tax_etf", "tax_daytrade", "rebate_rate",
    "commission_rate", "sec_rate", "sst_rate", "clearing_rate", "settlement_cap_rate",
})
_AMOUNT_FIELDS: frozenset[str] = frozenset({
    "min_fee", "commission_min", "platform_fee", "settlement_per_share", "cat_per_share",
    "sec_min", "taf_per_share", "taf_min", "broker_assisted_surcharge",
    "stamp_unit", "stamp_per_unit",
})
_CAP_FIELDS: frozenset[str] = frozenset({
    "taf_cap", "clearing_cap", "stamp_cap_stock", "stamp_cap_etf",
})
_ROUNDING_FIELD = "rounding"
_ROUNDING_CHOICES: frozenset[str] = frozenset({"floor", "half_up"})

EDITABLE_FIELDS: frozenset[str] = (
    _RATE_FIELDS | _AMOUNT_FIELDS | _CAP_FIELDS | {_ROUNDING_FIELD}
)

# Stable display / serialization order (grouped TW -> US -> MY -> stamp -> rounding). The API
# and the settings UI both iterate this so the field order is deterministic.
EDITABLE_FIELD_ORDER: tuple[str, ...] = (
    # TW commission + securities-transaction-tax
    "brokerage", "discount", "min_fee", "tax_normal", "tax_etf", "tax_daytrade", "rebate_rate",
    # US regulatory components
    "commission_rate", "commission_min", "platform_fee",
    "settlement_per_share", "settlement_cap_rate", "cat_per_share",
    "sec_rate", "sec_min", "taf_per_share", "taf_min", "taf_cap", "broker_assisted_surcharge",
    # MY commission / clearing / SST
    "clearing_rate", "clearing_cap", "sst_rate",
    # Stamp duty (MY native / US cross-currency)
    "stamp_unit", "stamp_per_unit", "stamp_cap_stock", "stamp_cap_etf",
    # Rounding mode
    _ROUNDING_FIELD,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")

_DDL = """
CREATE TABLE IF NOT EXISTS fee_rule_overrides (
    rule_set   TEXT PRIMARY KEY,
    overrides  TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class FeeOverrideError(ValueError):
    """A proposed override is invalid; carries the offending field for the API envelope."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class Overlay(NamedTuple):
    """One rule set's stored overlay: overridden field -> canonical string, + timestamp."""

    fields: dict[str, str]
    updated_at: str


def ensure_tables(conn: sqlite3.Connection) -> None:
    """Create the overlay table if absent (idempotent). Called by the WRITE paths only."""
    conn.execute(_DDL)
    conn.commit()


def _parse_decimal(field: str, value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        raise FeeOverrideError(f"{field} 須為數值字串", field=field)
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FeeOverrideError(f"{field} 非有效數值：{value!r}", field=field) from exc
    if not d.is_finite():
        raise FeeOverrideError(f"{field} 非有效數值：{value!r}", field=field)
    return d


def validate_field(field: str, value: object) -> str:
    """Validate + normalize a single override VALUE to its canonical stored string.

    ``value`` is never ``None`` here (null = revert is handled by :func:`set_overrides`).
    Raises :class:`FeeOverrideError` for an unknown/non-editable field, a non-numeric value,
    an out-of-range rate, a negative amount/cap, or a bad ``rounding`` mode.
    """
    if field not in EDITABLE_FIELDS:
        raise FeeOverrideError(f"未知或不可編輯的費率欄位：{field}", field=field)
    if field == _ROUNDING_FIELD:
        s = str(value)
        if s not in _ROUNDING_CHOICES:
            raise FeeOverrideError(
                f"捨入方式僅接受 floor 或 half_up：{value!r}", field=field)
        return s
    d = _parse_decimal(field, value)
    if field in _RATE_FIELDS:
        if d < _ZERO or d > _ONE:
            raise FeeOverrideError(f"{field} 須介於 0 與 1 之間（比率）：{value!r}", field=field)
    elif d < _ZERO:  # amount or cap
        raise FeeOverrideError(f"{field} 不可為負：{value!r}", field=field)
    return format(d, "f")


def overlay_for(conn: sqlite3.Connection, rule_set: str) -> Overlay | None:
    """The stored overlay for *rule_set*, or ``None`` when there is none.

    Degrades gracefully when the overlay table does not exist yet (no override has ever been
    saved): returns ``None`` WITHOUT creating the table, so a fee resolution never writes.
    """
    try:
        row = conn.execute(
            "SELECT overrides, updated_at FROM fee_rule_overrides WHERE rule_set = ?",
            (rule_set,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # table absent -> no overrides
    if row is None:
        return None
    try:
        data = json.loads(row["overrides"])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    fields = {str(k): str(v) for k, v in data.items() if k in EDITABLE_FIELDS}
    return Overlay(fields=fields, updated_at=str(row["updated_at"]))


def _apply_overrides(base: FeeRuleSet, fields: Mapping[str, str]) -> FeeRuleSet:
    """Return a copy of *base* with the whitelisted overlay *fields* applied (parsed)."""
    update: dict[str, Any] = {}
    for field, raw in fields.items():
        if field not in EDITABLE_FIELDS:
            continue  # ignore stale/unknown stored keys defensively
        if field == _ROUNDING_FIELD:
            if raw in _ROUNDING_CHOICES:
                update[field] = raw
            continue
        try:
            update[field] = Decimal(raw)
        except (InvalidOperation, ValueError):
            continue  # skip a corrupt stored value; keep the default
    if not update:
        return base
    return base.model_copy(update=update)


def apply_overlay(conn: sqlite3.Connection, rule_set: str, base: FeeRuleSet) -> FeeRuleSet:
    """The EFFECTIVE rule set = *base* (v2 default) merged with the DB overlay for *rule_set*."""
    overlay = overlay_for(conn, rule_set)
    if overlay is None:
        return base
    return _apply_overrides(base, overlay.fields)


def set_overrides(
    conn: sqlite3.Connection,
    rule_set: str,
    changes: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    """Apply a batch of field changes: a value = set (validated), ``None`` = revert (remove).

    Validation is atomic — a single bad field/value rejects the whole batch before any write.
    When the merged overlay becomes empty the row is deleted (equivalent to a per-set reset).
    The caller MUST pass a known *rule_set* (validated at the API layer against ``FEE_RULES``).
    """
    normalized: dict[str, str | None] = {}
    for field, value in changes.items():
        if value is None:
            if field not in EDITABLE_FIELDS:
                raise FeeOverrideError(f"未知或不可編輯的費率欄位：{field}", field=field)
            normalized[field] = None  # revert this field to default
        else:
            normalized[field] = validate_field(field, value)

    ensure_tables(conn)
    existing = overlay_for(conn, rule_set)
    merged: dict[str, str] = dict(existing.fields) if existing is not None else {}
    for field, norm in normalized.items():
        if norm is None:
            merged.pop(field, None)
        else:
            merged[field] = norm

    stamp = (now or datetime.now(UTC)).isoformat()
    if merged:
        conn.execute(
            "INSERT INTO fee_rule_overrides (rule_set, overrides, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(rule_set) DO UPDATE SET "
            "overrides = excluded.overrides, updated_at = excluded.updated_at",
            (rule_set, json.dumps(merged, ensure_ascii=False, sort_keys=True), stamp),
        )
    else:
        conn.execute("DELETE FROM fee_rule_overrides WHERE rule_set = ?", (rule_set,))
    conn.commit()


def reset(conn: sqlite3.Connection, rule_set: str) -> bool:
    """Delete the overlay row for *rule_set* (revert every field to v2 default). True if any."""
    ensure_tables(conn)
    cur = conn.execute("DELETE FROM fee_rule_overrides WHERE rule_set = ?", (rule_set,))
    conn.commit()
    return cur.rowcount > 0


def reset_all(conn: sqlite3.Connection) -> int:
    """Delete every overlay row (revert all rule sets to v2 defaults). Returns rows deleted."""
    ensure_tables(conn)
    cur = conn.execute("DELETE FROM fee_rule_overrides")
    conn.commit()
    return cur.rowcount
