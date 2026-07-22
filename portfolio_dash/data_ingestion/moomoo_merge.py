"""One-time, idempotent boot migration: merge the two legacy Moomoo accounts into one.

Batch B (2026-07-21). The two legacy accounts ``moomoo_my_us`` (US-settled) and
``moomoo_my_my`` (MY-settled) are collapsed into ONE dual-market account ``moomoo_my``
(settlement USD / funding MYR; per-market rule bindings US->moomoo_us/drip_us and
MY->moomoo_my/cash). This module RELABELS every account-scoped ledger row + rules binding
from the legacy ids to ``moomoo_my`` in ONE atomic transaction, on real prod data, at boot.

Design invariants (LOCKED — do not soften):

* **Runs only when BOTH legacy ids are present** in ``accounts`` (:func:`needs_moomoo_merge`).
  A second boot sees zero/one legacy ids and no-ops (returns ``False``).
* **Partial-release refusal:** if the DB is ready to migrate but the shipped config is NOT
  (``DEFAULT_ACCOUNTS`` still holds a legacy id or lacks ``moomoo_my``, or the
  ``account_market_rules`` table is missing), abort startup loudly rather than half-migrate.
* **Atomicity:** ONE explicit ``BEGIN IMMEDIATE`` … ``COMMIT`` span of plain DML. Any
  exception rolls the whole span back and re-raises (startup aborts pre-serving; the next
  boot re-runs cleanly). The connection is flipped to autocommit for the span so the explicit
  BEGIN/COMMIT are literal (``shared.db`` opens connections in Python's legacy
  implicit-transaction mode, which would otherwise inject its own BEGIN).
* **No double-count / no silent loss:** an in-span self-check proves the legacy ids are gone
  everywhere, that every per-currency cash-pool total is conserved, and that the merged
  account row has the right settlement/funding currencies — else it rolls back.

Layering: this module never imports ``ops`` (architecture.md — ``data_ingestion`` is below
``ops``). The pre-migration backup snapshot is orchestrated one level up, in ``api/app.py``,
gated by :func:`needs_moomoo_merge`.
"""

import sqlite3
from collections import defaultdict
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import DEFAULT_ACCOUNTS, AccountConfig

_MERGED_ID = "moomoo_my"
_LEGACY_IDS: tuple[str, str] = ("moomoo_my_us", "moomoo_my_my")
# Union scanned for the cash-pool continuity check (merged is empty pre-migration).
_CONTINUITY_ACCOUNTS: tuple[str, ...] = (*_LEGACY_IDS, _MERGED_ID)

# accounts-column tables that must hold ZERO legacy-id rows after the merge (ledger_audit is
# EXEMPT — its before_json is immutable history). data_source_fallbacks + pending_dividend_skips
# are checked separately (existence-guarded / TEXT-embedded id).
_ACCOUNT_ID_TABLES: tuple[str, ...] = (
    "transactions",
    "dividends",
    "fx_conversions",
    "cash_movements",
    "opening_inventory",
    "accounts",
    "account_market_rules",
)
# Ledgers relabelled by a plain UPDATE (surrogate PK, no account-scoped UNIQUE — verified
# against schema.py, so a blind account_id rewrite cannot collide).
_FLOW_TABLES: tuple[str, ...] = (
    "transactions",
    "dividends",
    "fx_conversions",
    "cash_movements",
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _merged_config() -> AccountConfig:
    """The ``moomoo_my`` :class:`AccountConfig` from the canonical config (pinned scalars +
    the two explicit market bindings). :func:`_assert_release_ready` has already proven it
    exists, so a missing entry here is an internal invariant break."""
    for a in DEFAULT_ACCOUNTS:
        if a.account_id == _MERGED_ID:
            return a
    raise RuntimeError(f"DEFAULT_ACCOUNTS unexpectedly lacks {_MERGED_ID!r}")


def needs_moomoo_merge(conn: sqlite3.Connection) -> bool:
    """True iff BOTH legacy Moomoo account ids are present in ``accounts`` (the S0 gate).

    Cheap read-only predicate: it drives BOTH the in-module S0 pre-flight and the app.py
    orchestration (snapshot + call), so exactly one snapshot is taken per actual migration.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE account_id IN (?, ?)", _LEGACY_IDS
    ).fetchone()
    return int(row[0]) == len(_LEGACY_IDS)


def _assert_release_ready(conn: sqlite3.Connection) -> None:
    """Refuse to migrate on a partial release (raise ``RuntimeError`` aborting startup)."""
    ids = {a.account_id for a in DEFAULT_ACCOUNTS}
    if _MERGED_ID not in ids:
        raise RuntimeError(
            f"moomoo merge blocked: DEFAULT_ACCOUNTS lacks {_MERGED_ID!r} "
            "(partial release — config not reshaped)"
        )
    leftover = ids & set(_LEGACY_IDS)
    if leftover:
        raise RuntimeError(
            f"moomoo merge blocked: DEFAULT_ACCOUNTS still contains legacy id(s) "
            f"{sorted(leftover)} (partial release)"
        )
    if not _table_exists(conn, "account_market_rules"):
        raise RuntimeError(
            "moomoo merge blocked: account_market_rules table missing (partial release)"
        )


def _cash_pool_sums(
    conn: sqlite3.Connection, account_ids: tuple[str, ...]
) -> dict[tuple[str, str], Decimal]:
    """Per-(bucket, currency) money totals across the account-scoped money ledgers.

    Summed in Python as exact ``Decimal`` (order-independent), so a pure relabel of
    ``account_id`` yields byte-identical totals before vs. after — the conservation guard.
    Currency comes natively from cash_movements/fx_conversions and via the instruments join
    for transactions/dividends (a symbol missing from ``instruments`` buckets under ``"?"``,
    identically before and after, so it never breaks the equality).
    """
    ph = ",".join("?" * len(account_ids))
    sums: dict[tuple[str, str], Decimal] = defaultdict(Decimal)

    for ccy, amount in conn.execute(
        f"SELECT ccy, amount FROM cash_movements WHERE account_id IN ({ph})", account_ids
    ):
        sums[("cash_movements", str(ccy))] += Decimal(str(amount))

    for from_ccy, from_amount, to_ccy, to_amount in conn.execute(
        f"SELECT from_ccy, from_amount, to_ccy, to_amount FROM fx_conversions "
        f"WHERE account_id IN ({ph})",
        account_ids,
    ):
        sums[("fx_from", str(from_ccy))] += Decimal(str(from_amount))
        sums[("fx_to", str(to_ccy))] += Decimal(str(to_amount))

    for ccy, net in conn.execute(
        f"SELECT i.quote_ccy, d.net FROM dividends d "
        f"LEFT JOIN instruments i ON i.symbol = d.symbol "
        f"WHERE d.account_id IN ({ph})",
        account_ids,
    ):
        key = str(ccy) if ccy is not None else "?"
        sums[("dividends_net", key)] += Decimal(str(net)) if net is not None else Decimal("0")

    for ccy, qty, price, fees, tax in conn.execute(
        f"SELECT i.quote_ccy, t.quantity, t.price, t.fees, t.tax FROM transactions t "
        f"LEFT JOIN instruments i ON i.symbol = t.symbol "
        f"WHERE t.account_id IN ({ph})",
        account_ids,
    ):
        key = str(ccy) if ccy is not None else "?"
        sums[("txn_notional", key)] += Decimal(str(qty)) * Decimal(str(price))
        sums[("txn_fees", key)] += Decimal(str(fees))
        sums[("txn_tax", key)] += Decimal(str(tax))

    return dict(sums)


def _run_migration(conn: sqlite3.Connection) -> None:
    """The DML body of the merge. Runs inside the caller's ``BEGIN IMMEDIATE`` span; any
    raise here propagates out to the ROLLBACK. Plain ``conn.execute`` only — no commits."""
    # Conservation baseline BEFORE any relabel (cash-pool continuity, V.b).
    pre_sums = _cash_pool_sums(conn, _CONTINUITY_ACCOUNTS)

    # U2 pre-check (F17): a symbol present under BOTH legacy accounts would collide on the
    # opening_inventory PK (account_id, symbol) once both re-key to moomoo_my — that means bad
    # source data, so ABORT the whole migration for human review (never OR IGNORE / OR REPLACE).
    collisions = conn.execute(
        "SELECT symbol FROM opening_inventory WHERE account_id = ? "
        "INTERSECT "
        "SELECT symbol FROM opening_inventory WHERE account_id = ?",
        _LEGACY_IDS,
    ).fetchall()
    if collisions:
        syms = ", ".join(sorted(str(r[0]) for r in collisions))
        raise RuntimeError(
            f"moomoo merge aborted: opening_inventory symbol(s) [{syms}] exist under BOTH "
            "legacy accounts (PK collision — bad data, human review required)"
        )

    # U1 — flow ledgers: plain relabel (surrogate PK, no account-scoped UNIQUE).
    for table in _FLOW_TABLES:
        conn.execute(
            f"UPDATE {table} SET account_id = ? WHERE account_id IN (?, ?)",
            (_MERGED_ID, *_LEGACY_IDS),
        )

    # U2 — opening_inventory: plain UPDATE. The pre-check ruled out collisions; SQLite's
    # default ABORT conflict behaviour stays as a loud backstop (never OR IGNORE/OR REPLACE).
    conn.execute(
        "UPDATE opening_inventory SET account_id = ? WHERE account_id IN (?, ?)",
        (_MERGED_ID, *_LEGACY_IDS),
    )

    # U3 (F04) — drop the legacy per-account fallback rows only; the (legacy, unused) table
    # itself is retained. Existence-guarded: this table is created later in boot / may be
    # absent on a minimal DB — absent => trivially zero legacy rows.
    if _table_exists(conn, "data_source_fallbacks"):
        conn.execute(
            "DELETE FROM data_source_fallbacks WHERE account_id IN (?, ?)", _LEGACY_IDS
        )

    # U4 — write the merged account row + its two market bindings (pinned scalars from the
    # canonical config), then delete the legacy account rows AND their legacy binding rows.
    cfg = _merged_config()
    conn.execute(
        "INSERT INTO accounts (account_id, name, broker, settlement_ccy, funding_ccy, "
        "fee_rule_set, dividend_model) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(account_id) DO UPDATE SET name=excluded.name, broker=excluded.broker, "
        "settlement_ccy=excluded.settlement_ccy, funding_ccy=excluded.funding_ccy, "
        "fee_rule_set=excluded.fee_rule_set, dividend_model=excluded.dividend_model",
        (
            cfg.account_id,
            cfg.name,
            cfg.broker,
            cfg.settlement_ccy.value,
            cfg.funding_ccy.value,
            cfg.fee_rule_set,
            cfg.dividend_model,
        ),
    )
    for b in cfg.market_bindings or []:
        conn.execute(
            "INSERT INTO account_market_rules (account_id, market, fee_rule_set, "
            "dividend_model) VALUES (?, ?, ?, ?) ON CONFLICT(account_id, market) DO UPDATE "
            "SET fee_rule_set=excluded.fee_rule_set, dividend_model=excluded.dividend_model",
            (cfg.account_id, b.market.value, b.fee_rule_set, b.dividend_model),
        )
    conn.execute("DELETE FROM accounts WHERE account_id IN (?, ?)", _LEGACY_IDS)
    conn.execute("DELETE FROM account_market_rules WHERE account_id IN (?, ?)", _LEGACY_IDS)

    # U5 (F08) — pending_dividend_skips: the account id is embedded in the TEXT fingerprint
    # PK (``div:<acct>:<symbol>:<YYYY-MM-DD>[:stock]``), invisible to a column scan. Rewrite
    # the legacy prefix to the merged one via INSERT-OR-IGNORE (both legacy accounts having
    # skipped the SAME detection collapse to one row — correct) + DELETE the old rows. An
    # EXACT substring match (not LIKE) is used deliberately: the legacy ids contain ``_``,
    # which LIKE would treat as a single-char wildcard (over-broad false match). Existence-
    # guarded — the table is created lazily by the dividend inbox and may be absent.
    if _table_exists(conn, "pending_dividend_skips"):
        merged_prefix = f"div:{_MERGED_ID}:"
        for legacy in _LEGACY_IDS:
            old_prefix = f"div:{legacy}:"
            conn.execute(
                "INSERT OR IGNORE INTO pending_dividend_skips (fingerprint, skipped_at) "
                "SELECT ? || substr(fingerprint, ?), skipped_at FROM pending_dividend_skips "
                "WHERE substr(fingerprint, 1, ?) = ?",
                (merged_prefix, len(old_prefix) + 1, len(old_prefix), old_prefix),
            )
            conn.execute(
                "DELETE FROM pending_dividend_skips WHERE substr(fingerprint, 1, ?) = ?",
                (len(old_prefix), old_prefix),
            )

    _self_check(conn, pre_sums)


def _self_check(
    conn: sqlite3.Connection, pre_sums: dict[tuple[str, str], Decimal]
) -> None:
    """In-span verification (V). Any failure raises -> the caller rolls the merge back."""
    # V.a — no legacy id survives in any account-scoped table (ledger_audit EXEMPT).
    for table in _ACCOUNT_ID_TABLES:
        n = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE account_id IN (?, ?)", _LEGACY_IDS
        ).fetchone()[0]
        if int(n) != 0:
            raise RuntimeError(f"moomoo merge self-check failed: {table} still has legacy rows")
    if _table_exists(conn, "data_source_fallbacks"):
        n = conn.execute(
            "SELECT COUNT(*) FROM data_source_fallbacks WHERE account_id IN (?, ?)",
            _LEGACY_IDS,
        ).fetchone()[0]
        if int(n) != 0:
            raise RuntimeError(
                "moomoo merge self-check failed: data_source_fallbacks still has legacy rows"
            )
    # V.a — pending_dividend_skips: scan the TEXT-embedded id via exact prefix (not LIKE).
    if _table_exists(conn, "pending_dividend_skips"):
        for legacy in _LEGACY_IDS:
            old_prefix = f"div:{legacy}:"
            n = conn.execute(
                "SELECT COUNT(*) FROM pending_dividend_skips WHERE substr(fingerprint, 1, ?) = ?",
                (len(old_prefix), old_prefix),
            ).fetchone()[0]
            if int(n) != 0:
                raise RuntimeError(
                    "moomoo merge self-check failed: pending_dividend_skips still has "
                    f"legacy fingerprints ({legacy})"
                )

    # V.b — cash-pool continuity: every per-currency total is conserved by the relabel.
    post_sums = _cash_pool_sums(conn, (_MERGED_ID,))
    if post_sums != pre_sums:
        raise RuntimeError(
            "moomoo merge self-check failed: cash-pool totals not conserved "
            f"(pre={pre_sums!r} post={post_sums!r})"
        )

    # V.c — the merged account row carries the correct currencies.
    row = conn.execute(
        "SELECT settlement_ccy, funding_ccy FROM accounts WHERE account_id = ?",
        (_MERGED_ID,),
    ).fetchone()
    if row is None or (str(row[0]), str(row[1])) != ("USD", "MYR"):
        raise RuntimeError(
            "moomoo merge self-check failed: merged account currencies are not USD/MYR"
        )


def migrate_moomoo_accounts(conn: sqlite3.Connection) -> bool:
    """Merge the two legacy Moomoo accounts into ``moomoo_my`` atomically; idempotent.

    Returns ``True`` when this call performed the migration, ``False`` when it was a no-op
    (the S0 gate saw fewer than both legacy ids). Raises (aborting startup) on a partial
    release or any mid-migration failure — the span is rolled back, leaving the DB unchanged,
    and the next boot re-runs cleanly.
    """
    if not needs_moomoo_merge(conn):
        return False  # S0: not both legacy ids present -> nothing to do (idempotent).
    _assert_release_ready(conn)  # partial-release guards raise before any write.

    # shared.db opens connections in Python's legacy implicit-transaction mode
    # (isolation_level == ""), which auto-injects a BEGIN before DML and would fight an
    # explicit BEGIN IMMEDIATE. Flip to autocommit for the span so BEGIN/COMMIT/ROLLBACK are
    # literal and the whole merge is exactly one transaction; restore the mode afterwards.
    prior_isolation = conn.isolation_level
    if conn.in_transaction:  # defensive: ensure BEGIN IMMEDIATE opens a fresh span
        conn.commit()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _run_migration(conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.isolation_level = prior_isolation
    return True
