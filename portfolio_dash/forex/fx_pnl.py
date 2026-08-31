"""Realized + unrealized FX P&L per account, and the reporting-currency rollup."""

from collections.abc import Callable, Sequence
from decimal import Decimal

from portfolio_dash.forex.pools import MovementRow, acquisition_basis, foreign_cash_balance
from portfolio_dash.forex.results import AccountFXResult, FxRealizedRow, FXSummary
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.fx import convert
from portfolio_dash.shared.models.assets import Account, Instrument
from portfolio_dash.shared.models.ledger import Dividend, FXConversion, Transaction

_ZERO = Decimal("0")
_ONE = Decimal("1")
SpotRate = Callable[[Currency, Currency], Decimal]


def _is_reconversion(c: FXConversion, home: Currency, foreign: Currency) -> bool:
    """True for a disposal of the foreign pool back into the home currency."""
    return c.from_ccy == foreign and c.to_ccy == home


def _realized_row(
    c: FXConversion, home: Currency, foreign: Currency, rate: Decimal,
) -> FxRealizedRow:
    """THE realized-FX formula. One site, so the two callers below cannot drift.

    ``home_received - (foreign_sold * rate)`` — deliberately NOT ``shared.fx.convert``,
    since ``rate`` is a derived pool rate, not a market spot.
    """
    return FxRealizedRow(
        date=c.date, foreign_ccy=foreign, home_ccy=home,
        foreign_sold=c.from_amount, home_received=c.to_amount,
        rate_used=rate,
        realized=c.to_amount - c.from_amount * rate,
    )


def realized_fx_rows(
    conversions: list[FXConversion], home: Currency, foreign: Currency,
    avg_rate: Decimal | None,
) -> list[FxRealizedRow]:
    """Per-reconversion realized FX rows priced at ONE caller-supplied rate.

    ⚠ **Prefer :func:`realized_fx_rows_as_of` for anything the owner reads as a realized
    figure.** Passing the pool's ALL-TIME average here prices every reconversion at a rate
    that keeps moving: an acquisition entered later restates a disposal already reported,
    and at a high enough later rate it flips its sign (QA-02, measured +10,000 -> -35,000
    on one February row). The manual (§8.2) defines the cost side as 「回換前 ... avg_rate」,
    which is what the ``_as_of`` variant computes.

    This form survives for the case it is actually right for: a caller that already knows
    the single rate it wants applied. Empty if ``avg_rate`` is None (no FX cost basis).
    """
    if avg_rate is None:
        return []
    return [_realized_row(c, home, foreign, avg_rate)
            for c in conversions if _is_reconversion(c, home, foreign)]


def realized_fx_rows_as_of(
    conversions: Sequence[FXConversion],
    movements: Sequence[MovementRow],
    home: Currency,
    foreign: Currency,
) -> list[FxRealizedRow]:
    """Per-reconversion realized FX rows, each priced at the pool average ON ITS OWN DATE.

    The cost of a disposal is fixed by the acquisitions that funded it — the ones dated on
    or before it (``acquisition_basis(..., as_of=c.date)``, which bounds conversions AND
    rated cash movements). A figure the owner has already read then stops moving when a
    later conversion is entered, which is the whole point (QA-02). The app is already
    date-aware for this same problem class on the equity side (``holdings.shares_through``)
    and on the cash side (``running_min``, audit C3).

    A reconversion with NO acquisition on or before its own date yields **no row**: that is
    the existing ``avg_rate is None -> []`` convention, applied per row instead of once per
    account. Pricing it with a rate first established a month afterwards would be guessing,
    and this codebase labels rather than guesses.
    """
    out: list[FxRealizedRow] = []
    for c in conversions:
        if not _is_reconversion(c, home, foreign):
            continue
        rate = acquisition_basis(
            conversions, movements, home, foreign, as_of=c.date).avg_rate
        if rate is None:
            continue
        out.append(_realized_row(c, home, foreign, rate))
    return out


def _realized_fx(
    conversions: list[FXConversion],
    movements: Sequence[MovementRow],
    home: Currency,
    foreign: Currency,
    avg_rate: Decimal | None,
) -> Decimal | None:
    """Sum realized FX P&L over reconversions (foreign -> home), each priced as at its date.

    ``avg_rate`` is the FULL-HISTORY pool average and is used only as the account-level
    None gate: no basis at all anywhere -> the account reports None, exactly as before.
    Once a basis exists the figure is a real sum, so an undatable reconversion contributes
    0 rather than voiding the whole account's number.
    """
    if avg_rate is None:
        return None
    total: Decimal = sum(
        (r.realized
         for r in realized_fx_rows_as_of(conversions, movements, home, foreign)), _ZERO
    )
    return total


def compute_account_fx(
    account: Account,
    foreign: Currency,
    foreign_stock_value: Decimal,
    transactions: list[Transaction],
    dividends: list[Dividend],
    conversions: list[FXConversion],
    instruments: dict[str, Instrument],
    spot: Decimal | None,
    movements: Sequence[MovementRow] | None = None,
) -> AccountFXResult:
    """FX P&L for one account (ledgers already scoped to it).

    ``foreign_stock_value`` is the current market value of equity holdings in the
    foreign currency (supplied by the portfolio core).
    ``spot`` is the current foreign->home exchange rate (None if unavailable).
    ``movements`` are the account's cash movements; foreign-currency ones now fund the
    pool and (when they carry ``acq_home_amount``) its cost basis — spec 2026-07-30.

    Unrealized figures are None when avg_rate is None (no basis at all) or spot is None.
    Realized figures are None when avg_rate is None; zero if no reconversions occurred.

    ``avg_rate`` (reported, and used for unrealized) is the FULL-HISTORY pool average;
    ``realized_fx`` prices each reconversion at the average AS AT ITS OWN DATE, so a
    conversion entered later cannot restate a figure already reported (QA-02, manual §8.2).
    """
    home = account.funding_ccy
    moves: Sequence[MovementRow] = movements or []
    # FULL-HISTORY basis: this is the mark-to-market side. ``avg_rate`` is the rate today's
    # remaining exposure was acquired at, and ``covered_ratio`` is a property of the pool as
    # it stands — neither is a statement about any one past disposal, so neither is bounded.
    basis = acquisition_basis(conversions, moves, home, foreign)
    avg_rate = basis.avg_rate
    ratio = basis.covered_ratio
    foreign_cash = foreign_cash_balance(
        transactions, dividends, conversions, instruments, foreign, movements=movements)
    # REALIZED is priced per row, as at each reconversion's own date (QA-02, manual §8.2).
    realized = _realized_fx(conversions, moves, home, foreign, avg_rate)

    # F2/F3: one ratio, applied to the WHOLE foreign exposure. Cash is fungible, so an
    # outflow draws proportionally from the basis-known and basis-unknown parts; and the
    # stock leg is scaled too because ``avg_rate`` itself came from the covered population.
    # ``ratio == _ONE`` skips the multiply so a fully-covered pool (every ledger written
    # before this spec) yields Decimals byte-identical to the previous engine.
    unreal_total: Decimal | None
    if avg_rate is None or spot is None:
        unreal_stocks: Decimal | None = None
        unreal_cash: Decimal | None = None
        unreal_total = None
    else:
        cash_base = foreign_cash if ratio == _ONE else foreign_cash * ratio
        stock_base = foreign_stock_value if ratio == _ONE else foreign_stock_value * ratio
        unreal_stocks = stock_base * (spot - avg_rate)
        unreal_cash = cash_base * (spot - avg_rate)
        # Combined unrealized FX computed with Decimal at the source, so the wire carries
        # the sum as a Decimal string and the frontend never re-adds the two components.
        # None whenever either component is None (they are always both-or-neither here).
        unreal_total = unreal_stocks + unreal_cash

    return AccountFXResult(
        account_id=account.account_id,
        home_ccy=home,
        foreign_ccy=foreign,
        avg_rate=avg_rate,
        current_spot=spot,
        foreign_cash=foreign_cash,
        foreign_stock_value=foreign_stock_value,
        realized_fx=realized,
        unrealized_fx_stocks=unreal_stocks,
        unrealized_fx_cash=unreal_cash,
        unrealized_fx_total=unreal_total,
        # Derived-on-the-server display values (the frontend computes neither).
        spot_delta=(spot - avg_rate if spot is not None and avg_rate is not None else None),
        covered_ratio=ratio,
        fx_basis_gap=(_ZERO if ratio == _ONE else foreign_cash * (_ONE - ratio)),
        fx_basis_incomplete=ratio != _ONE,
        foreign_cash_negative=foreign_cash < _ZERO,
    )


def _missing_rate_note(base: Currency, quote: Currency) -> str:
    """The user-facing name of an FX pair that has no stored rate.

    Worded exactly like ``portfolio/dashboard.py::RateResolver.rate``'s ``KeyError``, but
    COMPOSED HERE from the pair itself rather than read off the exception: ``current_spot``
    is an injected callable, so its message is not part of this module's contract (a test
    double would otherwise put a tuple repr in front of the owner).
    """
    return f"尚無 {base.value}/{quote.value} 匯率資料"


def _has_foreign_exposure(result: AccountFXResult) -> bool:
    """Does this account hold anything in the foreign currency, cash or stock?

    ZERO on both legs marks to market as zero at ANY spot, so omitting such an account
    from the rollup loses nothing and must stay silent — that is QA-01's empty
    ``moomoo_my``, whose absent ``MYR/TWD`` rate used to void the WHOLE FX section.
    Conversions/movements are deliberately NOT part of this predicate: a fully unwound
    pool has a provably zero unrealized figure, and its REALIZED side is judged on its own
    value a few lines below.
    """
    return result.foreign_cash != _ZERO or result.foreign_stock_value != _ZERO


def _rollup_reason(
    missing: dict[str, list[str]], *, realized_partial: bool, unrealized_partial: bool,
) -> str | None:
    """The C6 disclosure line: which accounts were skipped, why, and WHICH total is partial.

    Naming the partial figure is the point — realized and unrealized are excluded
    independently, and a reason that only said "some accounts were skipped" would leave the
    owner unable to tell which of the two numbers on screen is short.
    """
    figures = [name for name, partial
               in (("已實現", realized_partial), ("未實現", unrealized_partial)) if partial]
    if not missing or not figures:
        return None
    accounts = "、".join(f"{aid}（{'、'.join(missing[aid])}）" for aid in sorted(missing))
    return f"部分帳戶缺匯率已略過:{accounts} — {'與'.join(figures)}匯損益為部分合計"


def compute_fx_summary(
    accounts: dict[str, Account],
    instruments: dict[str, Instrument],
    transactions: list[Transaction],
    dividends: list[Dividend],
    fx_conversions: list[FXConversion],
    foreign_exposure: dict[str, tuple[Currency, Decimal]],
    current_spot: SpotRate,
    reporting: Currency,
    movements: Sequence[MovementRow] | None = None,
) -> FXSummary:
    """FX P&L for every FX-exposed account + reporting rollup.

    ``foreign_exposure`` maps account_id -> (foreign_ccy, foreign stock market value in
    that foreign ccy), supplied by the orchestrator from the portfolio core's valued
    holdings. Only accounts present in ``foreign_exposure`` are processed.

    ``current_spot(x, x)`` must return ``Decimal("1")`` (identity rate). BOTH rates it is
    asked for may be missing, and each degrades this account ONLY (QA-01 / QA-02,
    2026-08-29 — supersedes "a missing reporting rate is a configuration error ... allowed
    to raise", which assumed every account in ``foreign_exposure`` is active; the
    orchestrator puts every ``settlement_ccy != funding_ccy`` account here whether or not
    it holds anything, so one EMPTY account with no ``home -> reporting`` rate used to
    raise and take the caller's WHOLE ``fx`` section down with it):

    * **foreign -> home missing** — the unrealized legs are ``None`` (unchanged);
    * **home -> reporting missing** — this account cannot enter the rollup at all.

    Either way the rule is the same, and it is the one ``GET /api/cash`` already applies to
    its own reporting total (audit C6): an account with **nothing to add** is skipped in
    silence, because adding nothing changes nothing; an account with **something that
    cannot be expressed** is EXCLUDED and named in ``excluded_accounts`` +
    ``reporting_unavailable_reason``. Never a partial total presented as complete.
    """
    by_account: dict[str, AccountFXResult] = {}
    rep_realized = _ZERO
    rep_unrealized = _ZERO
    # account_id -> the missing pair(s) that kept it out, in discovery order.
    missing: dict[str, list[str]] = {}
    realized_partial = False
    unrealized_partial = False

    def _exclude(account_id: str, base: Currency, quote: Currency) -> None:
        note = _missing_rate_note(base, quote)
        pairs = missing.setdefault(account_id, [])
        if note not in pairs:
            pairs.append(note)

    for account_id, (foreign, stock_value) in foreign_exposure.items():
        account = accounts[account_id]
        home = account.funding_ccy
        txs = [t for t in transactions if t.account_id == account_id]
        divs = [d for d in dividends if d.account_id == account_id]
        convs = [c for c in fx_conversions if c.account_id == account_id]
        moves = [m for m in (movements or []) if m.account_id == account_id]
        try:
            spot: Decimal | None = current_spot(foreign, home)
        except KeyError:
            spot = None
        result = compute_account_fx(
            account, foreign, stock_value, txs, divs, convs, instruments, spot, moves
        )
        by_account[account_id] = result
        # home==reporting short-circuits to the identity rate so the rollup can't be broken
        # by an incomplete current_spot. Otherwise home->reporting gets the SAME guard the
        # foreign->home spot has always had (QA-01) — one account's gap, one account's loss.
        to_reporting: Decimal | None
        if home == reporting:
            to_reporting = _ONE
        else:
            try:
                to_reporting = current_spot(home, reporting)
            except KeyError:
                to_reporting = None

        realized = result.realized_fx
        # The server-computed combined leg: None whenever either component is None, which
        # is exactly the pair of conditions the two `is not None` guards used to test.
        unrealized = result.unrealized_fx_total
        if to_reporting is not None:
            if realized is not None:
                rep_realized += convert(realized, to_reporting)
            if unrealized is not None:
                rep_unrealized += convert(unrealized, to_reporting)
        else:
            # None or zero leaves the total unchanged either way, so it is skipped in
            # SILENCE; a real figure that cannot reach the reporting currency is money the
            # owner holds and this total does not show — disclose it (QA-02).
            if realized is not None and realized != _ZERO:
                realized_partial = True
                _exclude(account_id, home, reporting)
            if unrealized is not None and unrealized != _ZERO:
                unrealized_partial = True
                _exclude(account_id, home, reporting)

        # `unrealized is None` here means the account's OWN spot is missing (avg_rate is
        # not None, so the basis exists) — a figure the account HAS and could not mark to
        # market. Excluded, not zero. Gated on `avg_rate is not None` deliberately: with no
        # basis at all the spot is not the (only) blocker, and naming it would promise that
        # seeding one rate completes the figure. That case keeps its own cause-side signal
        # (`covered_ratio` / `fx_basis_incomplete`) on the account card.
        if spot is None and result.avg_rate is not None and _has_foreign_exposure(result):
            unrealized_partial = True
            _exclude(account_id, foreign, home)
    return FXSummary(
        by_account=by_account,
        reporting_currency=reporting,
        reporting_realized_fx=rep_realized,
        reporting_unrealized_fx=rep_unrealized,
        excluded_accounts=sorted(missing),
        reporting_unavailable_reason=_rollup_reason(
            missing, realized_partial=realized_partial, unrealized_partial=unrealized_partial
        ),
    )
