"""Computed FX (換匯) P&L result models."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.shared.enums import Currency


class AccountFXResult(BaseModel):
    """Per-account FX P&L. Money figures (realized/unrealized) are in ``home_ccy``;
    ``foreign_cash`` and ``foreign_stock_value`` are in ``foreign_ccy``.

    ``foreign_cash`` may be negative — net foreign drawn beyond the tracked conversions
    (e.g. an untracked funding path or sale proceeds). A negative balance is a signal the
    consumer should flag rather than render an FX figure on directly.
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
    # True when ``foreign_cash`` is NEGATIVE — the reconstructed foreign pool has been drawn
    # below zero, which means funding reached the account by a path this ledger does not
    # track (a direct foreign deposit that was never recorded). The unrealized FX figures are
    # still computed exactly as specified, but their CASH leg is then marking a balance that
    # does not exist, so the consumer MUST flag it rather than present the number bare
    # (audit M4; the ``foreign_cash`` docstring above has always asked for this).
    cash_basis_incomplete: bool = False


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
    """

    by_account: dict[str, AccountFXResult]
    reporting_currency: Currency
    reporting_realized_fx: Decimal
    reporting_unrealized_fx: Decimal
