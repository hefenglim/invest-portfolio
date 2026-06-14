"""Shared API wire mappers: enum case, Issue shape, fee-rule + dividend-model serialization."""

from typing import Any

from portfolio_dash.data_ingestion.config_seed import FeeRuleSet
from portfolio_dash.data_ingestion.validate import Issue
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.wire import decimal_str

_ISSUE_FIELD = {
    "sell_exceeds_holdings": "shares",
    "non_positive_quantity": "shares",
    "non_positive_price": "price",
    "unknown_account": "account_id",
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


def _tw_label(r: FeeRuleSet) -> str:
    return (
        f"{r.brokerage * 100}%・最低 NT${r.min_fee}・"
        f"賣出證交稅 {r.tax_normal * 100}%（ETF {r.tax_etf * 100}%）"
    )


def fee_rules_wire(r: FeeRuleSet) -> dict[str, Any]:
    """Serialize a FeeRuleSet to the frontend fee-rule shape (shared with spec 13)."""
    if r.market is Market.TW:
        label = _tw_label(r)
    elif r.market is Market.US:
        label = (
            f"平台費 USD {r.flat_fee}/筆"
            if r.flat_fee > 0
            else f"$0 佣金 + SEC fee {r.sec_fee}"
        )
    else:
        label = (
            f"佣金 {r.brokerage * 100}%・清算 {r.clearing * 100}%・"
            f"印花稅 {r.stamp_duty_rate * 100}%"
        )
    return {
        "rate": decimal_str(r.brokerage),
        "discount": decimal_str(r.discount),
        "min_fee": decimal_str(r.min_fee),
        "round_int": r.round_integer,
        "tax_sell": decimal_str(r.tax_normal),
        "tax_sell_etf": decimal_str(r.tax_etf),
        "label": label,
    }
