"""What-if trade simulation (spec 03 §3.2): a compute-only buy/sell preview.

Reuses the REAL fee/tax engine (``data_ingestion.fees.compute_fees``) so the numbers
match the actual write path, and the REAL ledger replay (``portfolio.build_book``) for
the held cost basis. It NEVER writes to any ledger table — this is a pure projection of
"what would the position look like after this trade".

The account binding follows Q1: an explicit ``account_id`` wins; otherwise the account
holding the MOST shares of the symbol. An unheld symbol with no ``account_id`` cannot be
priced into a rule set, so it raises ``WhatIfError`` (the router maps it to a 400).
"""

import sqlite3
from datetime import datetime
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.store import (
    list_dividends,
    list_instruments,
    list_opening,
    list_transactions,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.dashboard import RateResolver, build_dashboard
from portfolio_dash.portfolio.results import Holding
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, OpeningInventory, Transaction

_ZERO = Decimal("0")


class WhatIfError(ValueError):
    """Raised when the request cannot be simulated (e.g. unheld symbol, no account)."""


def _holding_for(holdings: list[Holding], account_id: str, symbol: str) -> Holding | None:
    """The (account, symbol) holding with shares > 0, or None if unheld."""
    for h in holdings:
        if h.account_id == account_id and h.symbol == symbol and h.shares > _ZERO:
            return h
    return None


def _most_shares_account(holdings: list[Holding], symbol: str) -> str | None:
    """The account holding the MOST shares of *symbol* (Q1), or None if unheld."""
    candidates = [h for h in holdings if h.symbol == symbol and h.shares > _ZERO]
    if not candidates:
        return None
    return max(candidates, key=lambda h: h.shares).account_id


def _fee_rule_set_name(conn: sqlite3.Connection, account_id: str) -> str | None:
    row = conn.execute(
        "SELECT fee_rule_set FROM accounts WHERE account_id=?", (account_id,)
    ).fetchone()
    return row["fee_rule_set"] if row is not None else None


def _fee_rule_desc(snapshot: dict[str, str], side: Side) -> str:
    """A short human-readable fee summary, best-effort from the fee-engine v2 snapshot.

    e.g. TW buy -> "0.1425%・最低 20"; TW sell adds "・證交稅 0.3%"; US/MY compose from the
    commission / platform / SEC-TAF / stamp components recorded in the snapshot.
    """
    def _pct(value: str) -> str:  # rate -> trimmed percentage, e.g. "0.001425" -> "0.1425"
        return f"{(Decimal(value) * 100).normalize():f}"

    parts: list[str] = []
    brokerage = snapshot.get("brokerage")  # TW commission rate
    if brokerage is not None and Decimal(brokerage) > _ZERO:
        parts.append(f"{_pct(brokerage)}%")
    commission_rate = snapshot.get("commission_rate")  # US/MY commission rate
    if commission_rate is not None and Decimal(commission_rate) > _ZERO:
        parts.append(f"佣金 {_pct(commission_rate)}%")
    platform = snapshot.get("platform")
    if platform is not None and Decimal(platform) > _ZERO:
        parts.append(f"平台費 {platform}")
    min_fee = snapshot.get("min_fee")
    if min_fee is not None and Decimal(min_fee) > _ZERO:
        parts.append(f"最低 {min_fee}")
    if side is Side.SELL:
        tax_rate = snapshot.get("tax_rate")  # TW securities-transaction tax
        if tax_rate is not None and Decimal(tax_rate) > _ZERO:
            parts.append(f"證交稅 {_pct(tax_rate)}%")
        if "sec" in snapshot or "taf" in snapshot:
            parts.append("SEC/TAF")
    if "stamp" in snapshot or "stamp_usd" in snapshot:
        parts.append("印花稅")
    return "・".join(parts) if parts else "無手續費/稅"


def compute_whatif(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    reporting: Currency,
    symbol: str,
    side: Side,
    shares: Decimal,
    price: Decimal,
    account_id: str | None,
) -> dict[str, str | bool | None]:
    """Simulate a buy/sell of *symbol* and return the projected position (compute-only).

    Raises:
        WhatIfError: symbol is unheld and no *account_id* was given (cannot infer account).
    """
    from portfolio_dash.data_ingestion.fees import compute_fees  # local: avoid cycle risk
    from portfolio_dash.data_ingestion.fx_lookup import resolve_stamp_fx

    # 1. Ledgers (Stored* rows -> ledger models) — same mapping as build_dashboard step 1.
    txs = [
        Transaction(account_id=s.account_id, symbol=s.symbol, side=s.side,
                    quantity=s.quantity, price=s.price, fees=s.fees, tax=s.tax,
                    trade_date=s.trade_date)
        for s in list_transactions(conn)
    ]
    divs = [
        Dividend(account_id=s.account_id, symbol=s.symbol, date=s.date,
                 type=DividendType(s.type), gross=s.gross, withholding=s.withholding,
                 net=s.net, reinvest_shares=s.reinvest_shares,
                 reinvest_price=s.reinvest_price)
        for s in list_dividends(conn)
    ]
    opening = [
        OpeningInventory(account_id=s.account_id, symbol=s.symbol, shares=s.shares,
                         original_avg_cost=s.original_avg_cost,
                         original_cost_total=s.original_cost_total,
                         build_date=s.build_date)
        for s in list_opening(conn)
    ]
    instruments = {i.symbol: i for i in list_instruments(conn)}
    book = build_book(txs, divs, opening, instruments)

    # 2. Resolve account (explicit wins; else most-shares; else cannot infer -> 400).
    resolved = account_id or _most_shares_account(book.holdings, symbol)
    if resolved is None:
        raise WhatIfError(
            f"無法推斷帳戶：{symbol} 未持有且未指定 account_id")
    rule_name = _fee_rule_set_name(conn, resolved)
    if rule_name is None:
        raise WhatIfError(f"未知帳戶 {resolved}")
    rules = get_fee_rule_set(rule_name)

    held = _holding_for(book.holdings, resolved, symbol)
    held_shares = held.shares if held is not None else _ZERO
    held_orig_total = held.original_cost_total if held is not None else _ZERO
    held_adj_total = held.adjusted_cost_total if held is not None else _ZERO
    held_adj_avg = (held_adj_total / held_shares) if held_shares > _ZERO else _ZERO

    inst = instruments.get(symbol)
    is_etf = inst.is_etf if inst is not None else False

    # 3. Fee/tax via the REAL engine — never re-implement the math. FE-D2 estimate path:
    # resolve the current USD/MYR rate for a Moomoo US MY stamp (silent omit if unavailable).
    stamp_fx = resolve_stamp_fx(conn, now.date()) if rules.has_us_stamp else None
    fr = compute_fees(rules, side, shares, price, is_etf=is_etf, stamp_fx=stamp_fx)
    fee = fr.fee
    tax = fr.tax
    amount = shares * price
    fee_rule_desc = _fee_rule_desc(fr.snapshot, side)

    out: dict[str, str | bool | None] = {
        "account_id": resolved,
        "amount": str(amount),
        "fee": str(fee),
        "tax": str(tax),
        "fee_rule_desc": fee_rule_desc,
    }

    if side is Side.BUY:
        total_cost = amount + fee + tax
        new_shares = held_shares + shares
        new_original_avg = (held_orig_total + total_cost) / new_shares
        new_adjusted_avg = (held_adj_total + total_cost) / new_shares
        out.update(
            total_cost=str(total_cost),
            new_shares=str(new_shares),
            new_original_avg=str(new_original_avg),
            new_adjusted_avg=str(new_adjusted_avg),
        )
        result_shares = new_shares
    else:  # SELL
        oversell = shares > held_shares
        proceeds_net = amount - fee - tax
        adjusted_cost_removed = held_adj_avg * shares
        realized = proceeds_net - adjusted_cost_removed
        remaining_shares = held_shares - shares
        out.update(
            proceeds_net=str(proceeds_net),
            adjusted_cost_removed=str(adjusted_cost_removed),
            realized=str(realized),
            remaining_shares=str(remaining_shares),
            oversell=oversell,
        )
        result_shares = remaining_shares

    out["new_weight"] = _new_weight(
        conn, now=now, reporting=reporting, symbol=symbol, inst=inst,
        held=held, result_shares=result_shares)
    return out


def _new_weight(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    reporting: Currency,
    symbol: str,
    inst: object,
    held: Holding | None,
    result_shares: Decimal,
) -> str | None:
    """The resulting position's reporting-ccy weight of the resulting total MV.

    Honest degradation: any missing current price or FX rate -> None, never fabricated.
    new_total = current_total - old_position_reporting_value + new_position_reporting_value.
    """
    if inst is None:
        return None
    quote_ccy: Currency = inst.quote_ccy  # type: ignore[attr-defined]

    dash = build_dashboard(conn, now=now, reporting=reporting)
    current_total = dash.kpis.total_market_value
    if current_total is None:
        return None

    # Current price of the symbol in its quote ccy (from any valued dashboard holding row).
    current_price: Decimal | None = None
    old_position_reporting_value = _ZERO
    for h in dash.holdings:
        if h.symbol == symbol and h.market_value is not None and h.shares > _ZERO:
            current_price = h.market_value / h.shares
            if h.account_id == (held.account_id if held is not None else None):
                old_position_reporting_value_q = h.market_value
                try:
                    old_position_reporting_value = convert(
                        old_position_reporting_value_q,
                        RateResolver(conn, now=now).rate(quote_ccy, reporting))
                except KeyError:
                    return None
    if current_price is None:
        return None

    try:
        rate = RateResolver(conn, now=now).rate(quote_ccy, reporting)
    except KeyError:
        return None
    new_position_value_quote = result_shares * current_price
    new_position_reporting_value = convert(new_position_value_quote, rate)
    new_total = current_total - old_position_reporting_value + new_position_reporting_value
    if new_total == _ZERO:
        return None
    return str(new_position_reporting_value / new_total)
