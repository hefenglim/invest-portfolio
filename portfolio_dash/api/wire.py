"""Shared API wire mappers: enum case, Issue shape, fee-rule + dividend-model serialization."""

import sqlite3
from collections.abc import Mapping
from typing import Any

from portfolio_dash.data_ingestion.config_seed import FeeRuleSet, get_fee_rule_set
from portfolio_dash.data_ingestion.validate import Issue
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.models.assets import MarketRule
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.wire import decimal_str

_ISSUE_FIELD = {
    "sell_exceeds_holdings": "shares",
    "non_positive_quantity": "shares",
    "non_positive_price": "price",
    "unknown_account": "account_id",
    "market_mismatch": "symbol",
    "amount_too_large": "shares",
    "negative_fee": "fee",
    "negative_tax": "tax",
    "fee_overflow": "shares",
}

_DIV_MODEL = {"cash_cost_reduction": "tw", "drip_us": "drip", "cash": "net"}


def parse_side(value: str) -> Side:
    """Accept lowercase/any-case wire side ('buy'/'sell') -> core Side enum."""
    return Side(value.strip().upper())


def issue_wire(issue: Issue) -> dict[str, Any]:
    """Map the core Issue to the frontend's {sev, code, text, field} shape."""
    return {
        "sev": "warn" if issue.needs_confirm else "error",
        "code": issue.kind,
        "text": issue.message,
        "field": _ISSUE_FIELD.get(issue.kind),
    }


def div_model_wire(dividend_model: str) -> str:
    """Map the stored accounts.dividend_model to the frontend div_model (tw/drip/net)."""
    return _DIV_MODEL.get(dividend_model, dividend_model)


def account_markets_wire(
    market_rules: Mapping[str, MarketRule], conn: sqlite3.Connection
) -> dict[str, dict[str, Any]]:
    """Per-market rule bundle for the Batch-B merged-account wire (ADDITIVE).

    Keyed by market VALUE ("US"/"TW"/"MY"); each entry carries the SAME ``fee_rules``
    shape (:func:`fee_rules_wire`) and ``div_model`` vocabulary (:func:`div_model_wire`)
    as the legacy per-account scalar fields, resolved per (account, market) from the
    account's ``market_rules`` bindings (``store.list_accounts`` populates them from the
    ``account_market_rules`` table).

    A SINGLE-market account yields exactly ONE entry mirroring its legacy scalar fields
    (the binding mirrors the scalar today), so any consumer that ignores ``markets`` — and
    every current account — sees byte-identical behaviour. A future merged dual-market
    account yields one entry per bound market so the frontend can book each market's
    dividends under the right model (F01) and show the right fee schedule.
    """
    return {
        market_val: {
            "fee_rules": fee_rules_wire(get_fee_rule_set(rule.fee_rule_set, conn)),
            "div_model": div_model_wire(rule.dividend_model),
        }
        for market_val, rule in market_rules.items()
    }


def _tw_label(r: FeeRuleSet) -> str:
    return (
        f"{r.brokerage * 100}%・最低 NT${r.min_fee}・"
        f"賣出證交稅 {r.tax_normal * 100}%（ETF {r.tax_etf * 100}%）"
    )


def _us_label(r: FeeRuleSet) -> str:
    # fee-engine v2: Moomoo US = commission + platform + settlement/CAT (+ SELL SEC/TAF);
    # Schwab = $0 online commission (+ SELL SEC/TAF).
    if r.platform_fee > 0:
        return (
            f"佣金 {r.commission_rate * 100}%・平台費 USD {r.platform_fee}/筆・"
            f"賣出 SEC/TAF"
        )
    return "$0 佣金・賣出 SEC/TAF"


def _my_label(r: FeeRuleSet) -> str:
    return (
        f"佣金 {r.commission_rate * 100}%・平台費 RM{r.platform_fee}・"
        f"清算 {r.clearing_rate * 100}%・SST {r.sst_rate * 100}%・"
        f"印花 ceil(金額/{r.stamp_unit})×RM{r.stamp_per_unit}"
    )


def fee_rules_wire(r: FeeRuleSet) -> dict[str, Any]:
    """Serialize a FeeRuleSet to the frontend fee-rule shape (shared with spec 13).

    The frontend consumes only ``label``; the remaining keys are the stable contract shape.
    ``round_int`` is True when the rule floors to integer NT$ (TW, fee-engine v2 FE-D3).
    """
    if r.market is Market.TW:
        label = _tw_label(r)
    elif r.market is Market.US:
        label = _us_label(r)
    else:
        label = _my_label(r)
    return {
        "rate": decimal_str(r.brokerage),
        "discount": decimal_str(r.discount),
        "min_fee": decimal_str(r.min_fee),
        "round_int": r.rounding == "floor",
        "tax_sell": decimal_str(r.tax_normal),
        "tax_sell_etf": decimal_str(r.tax_etf),
        "label": label,
    }
