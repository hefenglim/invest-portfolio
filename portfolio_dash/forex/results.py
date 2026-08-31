"""Computed FX (換匯) P&L result models."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from portfolio_dash.shared.enums import Currency


class AccountFXResult(BaseModel):
    """Per-account FX P&L. Money figures (realized/unrealized) are in ``home_ccy``;
    ``foreign_cash`` and ``foreign_stock_value`` are in ``foreign_ccy``.

    ``foreign_cash`` is the FULL foreign cash balance (it equals the funds view for the same
    pool since the 2026-07-30 spec). ``covered_ratio`` says how much of it carries a known
    acquisition cost; the unrealized legs are computed on the covered portion only.
    """

    account_id: str
    home_ccy: Currency
    foreign_ccy: Currency
    avg_rate: Decimal | None
    current_spot: Decimal | None
    foreign_cash: Decimal
    foreign_stock_value: Decimal
    realized_fx: Decimal | None
    unrealized_fx_stocks: Decimal | None
    unrealized_fx_cash: Decimal | None
    # Server-computed combined unrealized FX (in ``home_ccy``) = stocks + cash, or None
    # when EITHER component is None (no ``avg_rate`` / no current spot). This is the money
    # of record for 未實現匯損益（合計）: the frontend DISPLAYS this Decimal string and must
    # NEVER re-sum the two components client-side (adding two Decimal strings via JS
    # ``Number()`` is float money math over exact values — the locked invariant forbids it).
    # Additive field with a default so pre-existing AccountFXResult constructions still
    # validate; the sole real builder (``compute_account_fx``) always sets it explicitly.
    unrealized_fx_total: Decimal | None = None
    # ``current_spot - avg_rate`` (home per foreign), server-computed (audit L2, 2026-07-26).
    # Rates are formally not money, but the derived delta still belongs on this side of the
    # wire: the UI used to subtract two Decimal STRINGS via JS coercion, which yields a silent
    # NaN the moment either side is non-numeric. None when either rate is unavailable.
    spot_delta: Decimal | None = None
    # --- cost-basis coverage (spec 2026-07-30 F2/F3) ---------------------------------
    # Fraction of the pool whose acquisition cost is known. Exactly 1 when every foreign
    # funding flow carries a cost, which is the case for every ledger written before this
    # spec — so the unrealized legs below are then byte-identical to the previous engine.
    covered_ratio: Decimal = Decimal("1")
    # Foreign-currency amount that carries NO acquisition cost = foreign_cash * (1 - ratio).
    # NON-ZERO is the CAUSE-side alarm that replaced the old ``cash_basis_incomplete``
    # symptom flag: the old one only fired while the pool happened to be negative, so a
    # deposit smaller than the tracked conversions corrupted both legs in SILENCE (measured
    # live on 2026-07-30 — the pool crossed from -4,600.10 to +1,587.90 and the warning
    # vanished while the error stayed at 100,000 USD).
    fx_basis_gap: Decimal = Decimal("0")
    # The CAUSE-side boolean: some foreign funding has no acquisition cost. Consumers must
    # flag on THIS, not on ``fx_basis_gap != 0`` — the gap is an amount, so it collapses to
    # zero when the pool happens to be empty while ``covered_ratio`` (and therefore the
    # SCALED stock leg) is still incomplete. Flagging on the amount would go quiet exactly
    # like the negative-balance flag it replaced.
    fx_basis_incomplete: bool = False
    # True when ``foreign_cash`` is NEGATIVE. Kept as an INDEPENDENT second signal, but its
    # meaning changed: now that movements are counted, this equals the funds-view balance,
    # and withdrawals below zero are hard-blocked (``_withdraw_guard``). A negative pool is
    # therefore no longer an expected state — it means the ledger itself is inconsistent.
    foreign_cash_negative: bool = False


class FxRealizedRow(BaseModel):
    """One realized-FX event from a reconversion (foreign -> home)."""

    date: date
    foreign_ccy: Currency
    home_ccy: Currency
    foreign_sold: Decimal
    home_received: Decimal
    rate_used: Decimal
    realized: Decimal


class FXSummary(BaseModel):
    """All per-account results plus a reporting-currency rollup.

    Money figures are full-precision (not quantized); quantize at display/settlement.

    ⚠ The rollup is BEST-EFFORT and says so (QA-01 / QA-02, 2026-08-29). An account whose
    money cannot be expressed in the reporting currency — no ``foreign -> home`` spot, or
    no ``home -> reporting`` rate — is excluded from the two totals below and named in
    ``excluded_accounts``, instead of (a) raising and voiding the whole section or (b)
    being dropped in silence so a partial total reads as complete. Same shape and same
    reasoning as ``GET /api/cash``'s ``reporting_total_excluded`` /
    ``reporting_total_unavailable_reason`` (audit C6).
    """

    by_account: dict[str, AccountFXResult]
    reporting_currency: Currency
    reporting_realized_fx: Decimal
    reporting_unrealized_fx: Decimal
    # Accounts whose figures could NOT be rolled up, sorted (deterministic wire order).
    # Additive with a default that means COMPLETE, so every pre-existing FXSummary
    # construction still validates and still reads as a complete total.
    excluded_accounts: list[str] = Field(default_factory=list)
    # zh disclosure naming each excluded account, the FX pair that is missing for it, and
    # WHICH of the two totals is therefore partial (realized and unrealized are excluded
    # independently — a missing spot withholds only the unrealized side). Server-authored
    # and rendered verbatim; the web layer never composes it. None == nothing excluded.
    reporting_unavailable_reason: str | None = None
