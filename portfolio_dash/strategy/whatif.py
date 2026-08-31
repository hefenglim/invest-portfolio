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
from typing import Any

from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.rules_binding import fee_rule_for
from portfolio_dash.data_ingestion.store import load_ledger_bundle
from portfolio_dash.portfolio.cost_basis import (
    OversellError,
    UnbookableLedgerError,
    build_book,
)
from portfolio_dash.portfolio.dashboard import RateResolver, build_dashboard
from portfolio_dash.portfolio.results import Holding
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.enums import Side

_ZERO = Decimal("0")


class WhatIfError(ValueError):
    """Raised when the request cannot be simulated (e.g. unheld symbol, no account).

    Carries the wire detail the router needs (``field``, ``issues``) rather than a bare
    sentence. The router used to stamp EVERY case with ``field="account_id"`` — right for
    "cannot infer account", actively misleading for a blocking oversell that lives in some
    other account entirely and needs no change to the account_id the user sent (F-02).
    """

    def __init__(self, message: str, *, field: str | None = None,
                 issues: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.field = field
        self.issues = issues


def _holding_for(holdings: list[Holding], account_id: str, symbol: str) -> Holding | None:
    """The (account, symbol) holding, SIGNED, or None if nothing is held.

    ``!= _ZERO``, not ``> _ZERO`` (QA-04, 2026-08-29). An OPEN DECLARED SHORT is reported by
    ``build_book`` as a real position with a NEGATIVE share count and the received proceeds as
    its negative basis — it is not "unheld". The old filter dropped it, so the BUY arm read the
    position as absent and projected a FRESH LONG at this trade's own price on the very
    position the drawer renders two rows above as ``shares: "-10", short_open: true``: measured
    ``new_shares "6"`` / ``new_original_avg "150"`` where the ledger books ``-4`` at ``199.995``
    plus a ``+299.970`` short_cover. ``/api/input/manual/preview`` already answered that same
    trade correctly, so one app gave two answers for one trade.

    ``build_book`` never emits ``shares == 0`` (it drops flat positions), so the comparison is
    "is there a position at all". A NEGATIVE count here is always a declared short: an
    undeclared oversell raises ``OversellError`` out of the strict replay above, and the caller
    degrades to a 400 rather than quoting off a book whose basis was discarded.
    """
    for h in holdings:
        if h.account_id == account_id and h.symbol == symbol and h.shares != _ZERO:
            return h
    return None


def _most_shares_account(holdings: list[Holding], symbol: str) -> str | None:
    """The account holding the MOST shares of *symbol* (Q1), or None if unheld."""
    candidates = [h for h in holdings if h.symbol == symbol and h.shares > _ZERO]
    if not candidates:
        return None
    return max(candidates, key=lambda h: h.shares).account_id


def _fee_rule_set_name(
    conn: sqlite3.Connection, account_id: str, market: Market | None
) -> str | None:
    """Fee-rule-set name for (*account_id*, *market*); None if the account is unknown.

    Batch B: market-aware via the (account, market) binding table. LOCKED fallback — when
    *market* is None (unregistered symbol: no resolved instrument, hence no market to bind
    on) read the account scalar exactly as before, so the pre-swap behaviour is preserved.
    """
    if market is None:
        # No instrument (unregistered symbol): keep reading the account scalar as today.
        row = conn.execute(
            "SELECT fee_rule_set FROM accounts WHERE account_id=?", (account_id,)
        ).fetchone()
        return row["fee_rule_set"] if row is not None else None
    try:
        return fee_rule_for(conn, account_id, market)
    except KeyError:  # unknown account — preserve the pre-swap None return
        return None


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
    from portfolio_dash.data_ingestion.fees import (  # local: avoid cycle risk
        compute_fees,
        etf_flag_issue_applies,
        resolve_etf_flag,
    )
    from portfolio_dash.data_ingestion.fx_lookup import resolve_stamp_fx

    # 1. Ledgers — the same bundle build_dashboard step 1 loads.
    bundle = load_ledger_bundle(conn)
    instruments = bundle.instruments
    try:
        book = build_book(bundle)
    # ``InvalidOperation`` used to be the third member of this tuple, for the never-500 rule:
    # a decimal fault on ledger data is a user-fixable data problem like the other two, but it
    # is an ``ArithmeticError`` — NOT a ``ValueError`` — so it slipped past every degradation
    # site that catches ``(ValueError, KeyError)``. It was also ONE LEAF of that class:
    # ``decimal.Overflow`` and ``decimal.DivisionByZero`` are its siblings, so a
    # ``quantity='1E+999999'`` row still 500'd this door (measured, 2026-08-29). ``build_book``
    # now re-types the whole class into ``UnbookableLedgerError`` at the ONE place it is
    # raised, so the leaf is gone from here rather than widened: the arm below already
    # produces the envelope, and four call sites each remembering the same widening is how
    # three of them came to be missing it.
    except (UnbookableLedgerError, OversellError) as exc:
        # An un-bookable ledger is a user-fixable data problem, not a server fault.
        #
        # ⚠ `OversellError` is a SEPARATE hierarchy (`Exception`, not `ValueError`), so the
        # original one-class catch let it escape as a 500 — and the drawer posts 試算 on
        # open, so ONE oversold position anywhere in the ledger 500'd EVERY symbol's drawer,
        # including symbols in other accounts and other markets (found by W5, 2026-08-11,
        # and reproducible with no corporate action present — this is older than this
        # feature). `build_book` here is deliberately strict (`allow_oversell` defaults
        # False) because a 試算 must not quote a price off a book whose basis was discarded;
        # the fix is to degrade with the reason, not to relax the strictness.
        if isinstance(exc, OversellError):
            # The other half of this seam's OWN recorded fix. The comment above says the
            # 2026-08-11 repair was "to degrade with the reason"; only the 500-to-400 half
            # shipped, and the reason — already computed, right here — was dropped on the
            # floor by the router and then again by the drawer. The KPI band's XIRR, reading
            # the same book, has been naming it out loud the whole time.
            raise WhatIfError(
                f"帳本中有賣超部位待釐清（{exc.account_id}／{exc.symbol}，"
                f"{exc.trade_date.isoformat()}）— 無法試算，請先修正該筆交易",
                issues=[{
                    "sev": "error",
                    "code": "oversold_position",
                    "text": str(exc),
                    "field": None,
                    "account_id": exc.account_id,
                    "symbol": exc.symbol,
                    "trade_date": exc.trade_date.isoformat(),
                }],
            ) from exc
        raise WhatIfError(str(exc)) from exc

    # 2. Resolve account (explicit wins; else most-shares; else cannot infer -> 400).
    resolved = account_id or _most_shares_account(book.holdings, symbol)
    if resolved is None:
        raise WhatIfError(
            f"無法推斷帳戶：{symbol} 未持有且未指定 account_id", field="account_id")
    inst = instruments.get(symbol)
    # Market-aware fee rule (Batch B): pass the resolved instrument's market so a dual-market
    # account picks the market-appropriate rule set. An unregistered symbol (inst None) has no
    # market, so the helper keeps reading the account scalar exactly as before.
    rule_name = _fee_rule_set_name(
        conn, resolved, inst.market if inst is not None else None)
    if rule_name is None:
        raise WhatIfError(f"未知帳戶 {resolved}")
    rules = get_fee_rule_set(rule_name, conn)

    held = _holding_for(book.holdings, resolved, symbol)
    held_shares = held.shares if held is not None else _ZERO
    held_orig_total = held.original_cost_total if held is not None else _ZERO
    held_adj_total = held.adjusted_cost_total if held is not None else _ZERO

    # AI-D40: the same resolver both write doors use, so the drawer cannot quote a rate the
    # ledger would flag. ``inst`` is the registry row; an unregistered symbol is unanswered.
    is_etf, etf_unknown = resolve_etf_flag(inst, False)

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

    # OLD-vs-NEW pre-trade triple (R7 A4, C5): the held position as it stands, so the drawer
    # renders 持股/原始均價/調整均價 old→new. Null when nothing is held (held_shares == 0);
    # averages computed from totals on read (domain-ledger.md), never a stored rounded average.
    #
    # `!= _ZERO` (QA-04): an open short is SIGNED, so it reports `-10` and an average of
    # `-1999.950 / -10 = 199.995` — the average SALE price, which is what a negative basis over
    # a negative share count means. Printing 「—」 for it contradicted the position row the same
    # drawer had already rendered.
    if held_shares != _ZERO:
        out["old_shares"] = str(held_shares)
        out["old_original_avg"] = str(held_orig_total / held_shares)
        out["old_adjusted_avg"] = str(held_adj_total / held_shares)
    else:
        out["old_shares"] = None
        out["old_original_avg"] = None
        out["old_adjusted_avg"] = None

    # Declared once for BOTH arms: the BUY arm always has an average to give, the SELL arm
    # may not (a full exit leaves no position; an oversell forks into two different bases and
    # this surface has no declaration to read). One name, one meaning across the branch.
    new_original_avg: Decimal | None
    new_adjusted_avg: Decimal | None
    if side is Side.BUY:
        total_cost = amount + fee + tax
        new_shares = held_shares + shares
        if held_shares < _ZERO:
            # A buy against an OPEN SHORT covers it first (cost_basis.py, owner rule
            # 2026-07-31); it does not average into a long lot. Same operands, same order and
            # the same names as `api/routers/input_center.py::_position_preview`'s buy arm, so
            # the drawer and the trade form cannot give two answers for one trade (QA-04).
            short_shares = -held_shares
            short_avg = held_orig_total / held_shares    # neg/neg — the average sale price
            cover = min(shares, short_shares)
            per_share = total_cost / shares
            to_long = shares - cover
            realized_cover = (short_avg - per_share) * cover
            if to_long > _ZERO:
                # Short fully covered; the leftover starts its long life at THIS buy's cost.
                new_original = new_adjusted = per_share * to_long
            else:
                # Still short: the remaining proceeds, reduced pro rata by what was covered.
                new_original = held_orig_total + short_avg * cover
                new_adjusted = held_adj_total + short_avg * cover
            # Totals moved, THEN divided on read (domain-ledger.md). An exact cover leaves NO
            # position — the replay drops `shares == 0` — so it has no average at all, the same
            # null the SELL arm's full exit already returns.
            new_original_avg = None if new_shares == _ZERO else new_original / new_shares
            new_adjusted_avg = None if new_shares == _ZERO else new_adjusted / new_shares
            out.update(
                total_cost=str(total_cost),
                new_shares=str(new_shares),
                new_original_avg=(None if new_original_avg is None
                                  else str(new_original_avg)),
                new_adjusted_avg=(None if new_adjusted_avg is None
                                  else str(new_adjusted_avg)),
                covered_shares=str(cover),
                realized=str(realized_cover),
                realized_note="回補空單：以本次每股成本結算，剩餘股數以本次成本為起點",
            )
        else:
            new_original_avg = (held_orig_total + total_cost) / new_shares
            new_adjusted_avg = (held_adj_total + total_cost) / new_shares
            # The three cover fields are emitted as NULLs rather than omitted: the SELL arm
            # always sends `realized` / `realized_note`, and one BUY reply shaped differently
            # from another is how a renderer learns to test for a missing key instead of a
            # stated absence.
            out.update(
                total_cost=str(total_cost),
                new_shares=str(new_shares),
                new_original_avg=str(new_original_avg),
                new_adjusted_avg=str(new_adjusted_avg),
                covered_shares=None,
                realized=None,
                realized_note=None,
            )
        result_shares = new_shares
    else:  # SELL
        oversell = shares > held_shares
        proceeds_net = amount - fee - tax
        # SIGNED, and that is the point (QA-04): selling into an OPEN SHORT of 10 leaves −15,
        # not −5. `held_shares` used to be 0 for a short because `_holding_for` dropped it, so
        # the drawer printed the trade quantity as if the position started from flat. The share
        # count is the ONE figure both readings of an over-sell agree on — a declared short
        # extends the short lot to −15, an undeclared oversell nets `pos.shares` to −5 against
        # an untouched 10-share short lot, which `build_book` reports as −15 too — so it is
        # stated even where the basis and the realized amount are withheld below.
        remaining_shares = held_shares - shares
        realized_note: str | None = None
        if oversell:
            # The drawer has no 放空 declaration to read, and the two possible readings book
            # DIFFERENT realized amounts: a declared short realizes only the long portion and
            # opens a short lot for the rest, while an undeclared oversell discards the cost
            # basis entirely and books NOTHING (待釐清). Guessing either one prints a number
            # the ledger will not produce — this drawer showed the full proceeds as profit,
            # because held_adj_avg is 0 whenever held_shares <= 0 (review 2026-08-24, where
            # the same trade got +3,000 here and +500 on the manual form: one app, two
            # answers). So: state the fork, give no figure.
            adjusted_cost_removed = realized = None
            # The same fork, applied to the averages (sweep F-01, 2026-08-27): a declared
            # short would leave a SHORT lot averaged at the sale price, an undeclared oversell
            # DISCARDS the basis. Two branches, two different averages, and this surface has
            # no declaration to read — so it states the fork and gives no figure here either,
            # exactly as it does for the realized amount.
            new_original_avg = new_adjusted_avg = None
            realized_note = (
                "賣出股數超過持股：若為宣告放空，僅持有的部分結算已實現，其餘開立空單不產生"
                "已實現；若為賣超，成本基礎無法認定，帳本不會產生已實現損益（待釐清）。"
                "請用交易登錄頁勾選放空後檢視實際結果。")
        else:
            # Totals × frac, in build_book's own operand order — NOT avg × shares, which
            # re-divides and can differ from the booked row in the last digit.
            adjusted_cost_removed = held_adj_total * (shares / held_shares)
            realized = proceeds_net - adjusted_cost_removed
            # Totals moved, THEN divided on read (domain-ledger.md) — same operands and order
            # as build_book, so a partial exit's projected average is the pre-trade one to the
            # last digit rather than a second re-division of it. A FULL exit leaves no
            # position (the replay drops `shares == 0`), so it has no average at all.
            new_original_total = held_orig_total - held_orig_total * (shares / held_shares)
            new_adjusted_total = held_adj_total - adjusted_cost_removed
            new_original_avg = (None if remaining_shares == _ZERO
                                else new_original_total / remaining_shares)
            new_adjusted_avg = (None if remaining_shares == _ZERO
                                else new_adjusted_total / remaining_shares)
        out.update(
            proceeds_net=str(proceeds_net),
            adjusted_cost_removed=(None if adjusted_cost_removed is None
                                   else str(adjusted_cost_removed)),
            realized=None if realized is None else str(realized),
            realized_note=realized_note,
            remaining_shares=str(remaining_shares),
            oversell=oversell,
            new_original_avg=(None if new_original_avg is None else str(new_original_avg)),
            new_adjusted_avg=(None if new_adjusted_avg is None else str(new_adjusted_avg)),
        )
        result_shares = remaining_shares

    # Single dashboard pass surfaces BOTH weights (new + old) AND the current quote-ccy price,
    # so the SELL branch's 剩餘市值 needs no extra query (Senior Review #13).
    new_weight, old_weight, current_price = _new_weight(
        conn, now=now, reporting=reporting, symbol=symbol, inst=inst,
        held=held, result_shares=result_shares)
    if etf_flag_issue_applies(rules, side, etf_unknown):
        out["etf_flag_note"] = ("無法判定是否為 ETF,此試算暫以現股稅率計算,"
                                "實際稅率待你在標的管理確認")
    else:
        out["etf_flag_note"] = None
    out["new_weight"] = new_weight
    out["old_weight"] = old_weight
    if side is Side.SELL:
        # 剩餘市值 in the QUOTE ccy = max(remaining, 0) × current price (server-side; keeps the
        # drawer's no-local-money-math rule). Floors at 0 so an oversell shows no negative value;
        # None when the price is unavailable (honest degradation, never fabricated).
        rem = result_shares if result_shares > _ZERO else _ZERO
        out["remaining_market_value"] = (
            str(rem * current_price) if current_price is not None else None)
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
) -> tuple[str | None, str | None, Decimal | None]:
    """Return ``(new_weight, old_weight, current_price)`` for the position (C5 refactor).

    * ``new_weight`` — the resulting position's reporting-ccy weight of the resulting total MV
      (new_total = current_total − old_position_reporting_value + new_position_reporting_value).
    * ``old_weight`` — the CURRENT position's reporting-ccy weight of the current total MV
      (old_position_reporting_value / current_total), surfaced from the SAME dashboard pass
      (operands already exist internally — no duplicate dashboard build, Senior Review #9).
    * ``current_price`` — the symbol's current QUOTE-ccy price (market_value / shares), reused
      by the SELL branch's 剩餘市值.

    Honest degradation: any missing current price or FX rate -> None for the affected field(s),
    never fabricated.

    ⚠ The dashboard scan below keeps its ``shares > 0`` filter, so a position whose ONLY
    holding of *symbol* is an open SHORT yields no ``current_price`` and every field here
    degrades to None (QA-04 fix, deliberate scope). ``domain-ledger.md`` records that
    allocation weights over a net-short portfolio "can exceed 100% or sign-flip" — an honest
    reading of a degenerate input, but one this card has no room to explain, and 「—」 says
    less than a signed percentage that needs a paragraph. The MONEY fields (shares, both
    averages, realized, covered) are computed for the short in every branch above; only the
    weight/剩餘市值 pair abstains, and it abstained before this fix too.
    """
    if inst is None:
        return None, None, None
    quote_ccy: Currency = inst.quote_ccy  # type: ignore[attr-defined]

    dash = build_dashboard(conn, now=now, reporting=reporting)
    current_total = dash.kpis.total_market_value
    if current_total is None:
        return None, None, None

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
                    return None, None, current_price
    if current_price is None:
        return None, None, None

    old_weight = (
        None if current_total == _ZERO
        else str(old_position_reporting_value / current_total))
    try:
        rate = RateResolver(conn, now=now).rate(quote_ccy, reporting)
    except KeyError:
        return None, old_weight, current_price
    new_position_value_quote = result_shares * current_price
    new_position_reporting_value = convert(new_position_value_quote, rate)
    new_total = current_total - old_position_reporting_value + new_position_reporting_value
    if new_total == _ZERO:
        return None, old_weight, current_price
    return str(new_position_reporting_value / new_total), old_weight, current_price
