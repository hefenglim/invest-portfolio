"""US equity options — the ONE owner of the contract symbol, the multiplier and the vocabulary.

Spec ``docs/spec/2026-10-09-options-ledger.md`` (approach ④, owner rulings R1–R7, OPT-D1..D22).
A contract is an ordinary instrument in the trade ledger; its quantity is in
**share-equivalents** (contracts × :data:`MULTIPLIER`, OPT-D1) and its price is the premium per
share, so every ``quantity × price`` in the package stays correct untouched. Everything a
contract *is* — underlying, expiry, strike, right — is read from its OCC symbol by
:func:`parse_option_symbol` (OPT-D22: no terms table, the symbol carries every term).

Pure: stdlib only. ``shared/`` modules may import each other, but this one imports nothing
internal at all, so every layer — ``data_ingestion``, ``portfolio``, ``api``, ``export`` and the
broker converter — can reach it without an edge (plan invariant 3,
``tests/shared/test_options.py`` guards it).

One definition per rule (plan invariant 6) — nothing below may be re-derived elsewhere:

* the symbol: :data:`OPTION_PATTERN`, :func:`is_option_symbol`, :func:`parse_option_symbol`,
  :func:`option_symbol` (the only composer — the browser never composes one, it calls the
  compose endpoint), :func:`option_display_name`, :func:`from_broker_option`;
* the multiplier: :func:`contracts_to_shares`, :func:`shares_to_contracts`,
  :func:`is_contract_multiple`;
* OPT-D11 / OPT-D16: :func:`nonstandard_adjustment_reason` / :func:`is_standard_adjustment`;
* §6.1: :class:`OptionSide` and :func:`classify_option_rows`, the zh table, and the notes.

TODO(options-⑤): approach ⑤ — a dedicated options sub-ledger (multi-leg grouping, roll chains,
collateral reservation, quotes and Greeks, a non-standard deliverable model with its own terms
table, option alerts). Started only when the owner asks; the plan of record is spec §12.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal, NamedTuple

#: Shares delivered per contract. v1 models the standard deliverable only (OPT-D11 / OPT-D16).
MULTIPLIER = 100
_M = Decimal(MULTIPLIER)
_THOUSAND = Decimal(1000)
_ZERO = Decimal(0)
_ONE = Decimal(1)
#: The strike field is eight digits of strike × 1000.
_STRIKE_LIMIT = Decimal(10) ** 8

#: OCC without root padding: a letters-only root, ``YYMMDD``, ``C``/``P``, strike × 1000 as eight
#: digits — ``XYZ261218P00050000``. Letters only: an adjusted contract's root carries a digit
#: (``XYZ1``) and a TW / MY code is all digits; both are out of v1 (OPT-D11, R6).
OPTION_PATTERN = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
_PARTS = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")
_ROOT = re.compile(r"^[A-Z]{1,6}$")
#: A broker statement's contract string: ``XYZ 12/18/2026 50.00 P`` (Schwab).
_BROKER = re.compile(r"^([A-Z]{1,6}) (\d{2})/(\d{2})/(\d{4}) (\d+(?:\.\d+)?) ([CP])$")

Right = Literal["C", "P"]
AssetType = Literal["stock", "option"]
ASSET_STOCK: AssetType = "stock"
ASSET_OPTION: AssetType = "option"
#: ``transactions.close_reason`` — a price-0 closing row (OPT-D2).
CloseReason = Literal["expired", "assigned", "exercised"]
CLOSE_REASONS: frozenset[str] = frozenset({"expired", "assigned", "exercised"})


@dataclass(frozen=True)
class OptionTerms:
    """One contract's terms, as its symbol states them."""

    underlying: str
    expiry: date
    strike: Decimal
    right: Right
    multiplier: int = MULTIPLIER


class OptionSymbolError(ValueError):
    """A contract that cannot be written as an OCC symbol; the message is the zh refusal."""


def _plain(value: Decimal) -> str:
    """A Decimal as fixed-point text without trailing zeros — ``50``, ``7.5``, never ``5E+1``."""
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _is_positive_integer(value: Decimal) -> bool:
    """The ratio-term rule of ``shared/corporate_actions.py::is_ratio_term``, restated here
    because this module imports nothing internal; ``test_options`` pins the two together."""
    return value.is_finite() and value > _ZERO and value == value.to_integral_value()


def parse_option_symbol(symbol: str) -> OptionTerms | None:
    """The terms of an OCC contract symbol, or ``None`` for anything that is not one.

    Case and surrounding space are normalised. A malformed date or a zero strike is not a
    contract either, so the shape check alone never decides."""
    m = _PARTS.match(symbol.strip().upper())
    if m is None:
        return None
    root, yy, mm, dd, right, digits = m.groups()
    try:
        expiry = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return None
    strike = Decimal(int(digits)) / _THOUSAND
    if strike <= _ZERO:
        return None
    return OptionTerms(underlying=root, expiry=expiry, strike=strike,
                       right="C" if right == "C" else "P")


def is_option_symbol(symbol: str) -> bool:
    """*symbol* has the OCC contract SHAPE (after strip + upper) — the symbol-only form of
    ``Instrument.is_option``. The two never disagree: every registering door sets
    ``asset_type`` from the symbol's parse, and the stock doors refuse this shape at entry.
    Readers that hold only a symbol use this; readers holding an ``Instrument`` use
    ``.is_option``."""
    return OPTION_PATTERN.match(symbol.strip().upper()) is not None


def option_symbol(terms: OptionTerms) -> str:
    """The OCC symbol for *terms* — the ONLY composer. Raises :class:`OptionSymbolError` with
    the zh refusal the compose endpoint returns (digit root, malformed strike, non-US)."""
    root = terms.underlying.strip().upper()
    if _ROOT.match(root) is None:
        raise OptionSymbolError(
            f"選擇權僅支援美股：標的代號須為 1–6 個英文字母（收到「{terms.underlying}」）")
    if terms.multiplier != MULTIPLIER:
        raise OptionSymbolError(f"僅支援標準合約（每口 {MULTIPLIER} 股）")
    right = str(terms.right).strip().upper()
    if right not in ("C", "P"):
        raise OptionSymbolError("請選擇買權（C）或賣權（P）")
    if not 2000 <= terms.expiry.year <= 2099:
        raise OptionSymbolError("到期日須介於 2000 與 2099 年之間")
    strike = terms.strike
    scaled = strike * _THOUSAND if strike.is_finite() else _ZERO
    if (not strike.is_finite() or strike <= _ZERO or scaled != scaled.to_integral_value()
            or scaled >= _STRIKE_LIMIT):
        raise OptionSymbolError(
            "履約價格式不正確：須為大於 0 的數字，最多 3 位小數，且小於 100,000")
    return f"{root}{terms.expiry.strftime('%y%m%d')}{right}{int(scaled):08d}"


def option_display_name(terms: OptionTerms) -> str:
    """「XYZ 2026-12-18 50 Put」 — the contract's name on every surface (spec §3). The right
    keeps the statement's English word (§6.1); the column headers say 買權／賣權."""
    word = "Call" if terms.right == "C" else "Put"
    return f"{terms.underlying} {terms.expiry.isoformat()} {_plain(terms.strike)} {word}"


def contract_name(symbol: str) -> str | None:
    """The display name of an OCC *symbol*, or ``None`` when it is not a contract."""
    terms = parse_option_symbol(symbol)
    return None if terms is None else option_display_name(terms)


def from_broker_option(text: str) -> OptionTerms | None:
    """The terms of a broker statement's contract string (``XYZ 12/18/2026 50.00 P``), or
    ``None`` when it is not a standard contract (a digit root, a bad date, a zero strike).

    Round-tripped through :func:`option_symbol` so the strike comes back canonical (``50``,
    not ``50.00``) and every limit the composer enforces holds here too."""
    m = _BROKER.match(" ".join(text.strip().upper().split()))
    if m is None:
        return None
    root, mm, dd, yyyy, strike, right = m.groups()
    try:
        terms = OptionTerms(underlying=root, expiry=date(int(yyyy), int(mm), int(dd)),
                            strike=Decimal(strike), right="C" if right == "C" else "P")
        return parse_option_symbol(option_symbol(terms))
    except (ValueError, ArithmeticError):
        return None


# --- units (OPT-D1 / OPT-D8) --------------------------------------------------------------


def contracts_to_shares(contracts: Decimal | int) -> Decimal:
    """口數 → share-equivalents (the ledger's unit)."""
    return Decimal(contracts) * _M


def shares_to_contracts(shares: Decimal) -> Decimal:
    """Share-equivalents → 口數. Exact; a non-multiple yields a fraction — doors check
    :func:`is_contract_multiple` first."""
    return shares / _M


def is_contract_multiple(shares: Decimal) -> bool:
    """A positive whole number of contracts in share-equivalents (「選擇權數量須為 100 的倍數」)."""
    if not shares.is_finite() or shares <= _ZERO:
        return False
    contracts = shares / _M
    return contracts == contracts.to_integral_value()


# --- OPT-D11 / OPT-D16: standard adjustments only ------------------------------------------


def strike_products(from_terms: OptionTerms, to_terms: OptionTerms, ratio_to: Decimal,
                    ratio_from: Decimal) -> tuple[Decimal, Decimal]:
    """``(strike_from × ratio_from, strike_to × ratio_to)`` — the conservation pair the
    adjustment form prints (「履約價×口數：60×1 ＝ 20×3 ✓」) without browser arithmetic."""
    return from_terms.strike * ratio_from, to_terms.strike * ratio_to


def nonstandard_adjustment_reason(from_terms: OptionTerms, to_terms: OptionTerms,
                                  ratio_to: Decimal, ratio_from: Decimal) -> str | None:
    """``None`` when ``from → to`` at ``ratio_to : ratio_from`` is a standard adjustment, else
    the zh reason it is not (OPT-D11 + OPT-D16): both contracts deliver 100 shares, the
    destination root is letters only (a parsed symbol guarantees it), same right, same expiry,
    ``ratio_from == 1`` (1 → N contracts — anything else leaves a fractional contract) and
    ``strike_from × ratio_from == strike_to × ratio_to``."""
    if from_terms.multiplier != MULTIPLIER or to_terms.multiplier != MULTIPLIER:
        return f"選擇權合約調整：僅支援標準合約（每口 {MULTIPLIER} 股）"
    if _ROOT.match(to_terms.underlying) is None:
        return "選擇權合約調整：新合約的標的代號須為 1–6 個英文字母"
    if not (_is_positive_integer(ratio_to) and _is_positive_integer(ratio_from)):
        return "選擇權合約調整：口數比例須為正整數"
    if ratio_from != _ONE:
        return "選擇權合約調整以 1 口為基準，請填 1 → N"
    if from_terms.right != to_terms.right:
        return "選擇權合約調整：調整前後須同為買權或同為賣權"
    if from_terms.expiry != to_terms.expiry:
        return "選擇權合約調整：調整前後的到期日須相同"
    before, after = strike_products(from_terms, to_terms, ratio_to, ratio_from)
    if before != after:
        return (f"選擇權合約調整：履約價×口數須相等（{_plain(from_terms.strike)}×"
                f"{_plain(ratio_from)} ≠ {_plain(to_terms.strike)}×{_plain(ratio_to)}）")
    return None


def is_standard_adjustment(from_terms: OptionTerms, to_terms: OptionTerms,
                           ratio_to: Decimal, ratio_from: Decimal) -> bool:
    """OPT-D11 / OPT-D16 as a predicate; :func:`nonstandard_adjustment_reason` says why not."""
    return nonstandard_adjustment_reason(from_terms, to_terms, ratio_to, ratio_from) is None


# --- §6.1 vocabulary — the only spelling of every option word ------------------------------


class OptionSide(StrEnum):
    """A trade row's option side (OPT-D15). The value IS the zh label every surface prints.

    買進平倉 and 買進開倉 are both a bare BUY in the ledger, so the side is never stored: it is
    derived by :func:`classify_option_rows`, on the server, for every surface at once."""

    STO = "賣出開倉"
    BTC = "買進平倉"
    BTO = "買進開倉"
    STC = "賣出平倉"
    #: A BUY that covers the whole short and opens a long with the rest (CSV / edit rows).
    BTC_BTO = "買進平倉＋買進開倉"
    #: A declared SELL that sells the whole long and opens a short with the rest.
    STC_STO = "賣出平倉＋賣出開倉"
    #: An undeclared SELL beyond the long — the ordinary 賣超 (待釐清).
    STC_OVERSOLD = "賣出平倉（賣超）"
    EXPIRED = "到期作廢"
    ASSIGNED = "被指派"
    EXERCISED = "履約"
    #: A close row the replay refused (§4.4): it books nothing.
    UNBOOKED = "未入帳（待釐清）"


DIRECTION_ZH: dict[str, str] = {"long": "買方", "short": "賣方"}
UNIT_CONTRACT = "口"
UNIT_SHARE_EQUIVALENT = "股當量"
PREMIUM_ZH = "權利金（每股）"
STRIKE_ZH = "履約價"
OBLIGATION_ZH = "履約義務"
RIGHT_ZH: dict[str, str] = {"C": "買權", "P": "賣權"}
CLOSE_REASON_ZH: dict[str, str] = {"expired": "到期作廢", "assigned": "被指派",
                                   "exercised": "履約"}
#: ``instruments.asset_type`` in words (the ledger-audit reader prints a before-image).
ASSET_TYPE_ZH: dict[str, str] = {"stock": "股票", "option": "選擇權"}
#: The corporate-action kind's word — ``shared/corporate_actions.py::KIND_ZH`` reads it.
ADJUST_KIND_ZH = "選擇權合約調整"

#: R3 — under the 選擇權 section and the trend chart when a trend day skipped an option.
TREND_EXCLUDES_OPTIONS_NOTE = (
    "走勢不含未平倉選擇權的市值；權利金已計入現金，未平倉期間淨值高估（賣方）或低估（買方）該部位市值")
#: OPT-D20 — why 本金匯率效果 reads 「—」 while an option is open.
PRINCIPAL_FX_OPTION_NOTE = (
    "有未平倉選擇權時，含匯兌總損益不含其市值而資產損益含其未實現，兩者相減無法拆出本金匯率效果；"
    "平倉或到期後恢復顯示")
#: §4.4 — the remedy every surface prints beside a refused close.
REFUSED_CLOSE_REMEDY = "刪除該筆到期作廢／被指派／履約列，或補回被刪除的交易"

_CLOSE_SIDE: dict[str, OptionSide] = {"expired": OptionSide.EXPIRED,
                                      "assigned": OptionSide.ASSIGNED,
                                      "exercised": OptionSide.EXERCISED}


# --- §6.1 classifier ------------------------------------------------------------------------

#: Same-day order — an adjustment re-denominates the position before that day's trades, which
#: are quoted in post-adjustment terms. ``shared/ledger_events.py::EventPriority`` owns the
#: ranks (CORPORATE_ACTION 10 < TRADE 20); this module may not import it, so only the ORDER is
#: restated, and ``test_options`` pins it to the enum.
_ACTION_RANK = 0
_TRADE_RANK = 1


class OptionRow(NamedTuple):
    """One stored option trade row, as the classifier reads it."""

    id: int
    account_id: str
    symbol: str
    trade_date: date
    #: ``"BUY"`` / ``"SELL"`` — a ``Side`` is a ``StrEnum`` and compares equal.
    side: str
    #: Share-equivalents.
    quantity: Decimal
    short_sale: bool
    close_reason: str | None


class OptionAdjustRow(NamedTuple):
    """One readable ``OPTION_ADJUST`` row (1 → ``ratio_to`` contracts)."""

    id: int
    account_id: str
    date: date
    from_symbol: str
    to_symbol: str
    ratio_to: Decimal


@dataclass
class _Lots:
    """The replay's two lots for one (account, contract): ``long`` (negative after a 賣超) and
    a positive ``short``, plus the sticky 賣超 flag."""

    long: Decimal = _ZERO
    short: Decimal = _ZERO
    oversold: bool = False


def classify_option_rows(rows: Iterable[OptionRow],
                         actions: Iterable[OptionAdjustRow]) -> dict[int, OptionSide]:
    """Every option trade row's :class:`OptionSide`, keyed by row id — the ONE definition.

    One walk in the replay's order ``(date, priority, id)`` keeps, per (account, contract),
    the replay's long lot, short lot and 賣超 flag, and mirrors the replay's branches:

    * a BUY covers the short lot first and adds the rest to the long lot;
    * a declared SELL sells the long lot first and opens / extends the short lot with the rest;
    * an undeclared SELL beyond the long lot is a 賣超 (the lot goes negative, sticky);
    * a ``close_reason`` row that would open or extend a lot — a BUY beyond the short lot, a
      SELL beyond the long lot, or a close against the direction — is refused (§4.4): labelled
      未入帳（待釐清） and moves nothing;
    * an ``OPTION_ADJUST`` (before the day's trades) moves both lots × ``ratio_to`` to the
      destination, unless the replay would refuse it — no source position, an oversold source
      or destination, a destination open in the opposite direction, or ``from == to`` — in
      which case it moves nothing.

    Callers pass the option rows of the ledger they label (every account at once is fine:
    each (account, contract) walks on its own) and the readable ``OPTION_ADJUST`` rows. A row
    of zero quantity moves nothing, like the replay's own skip.
    """
    events: list[tuple[date, int, int, OptionRow | OptionAdjustRow]] = []
    for r in rows:
        events.append((r.trade_date, _TRADE_RANK, r.id, r))
    for a in actions:
        events.append((a.date, _ACTION_RANK, a.id, a))
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    lots: dict[tuple[str, str], _Lots] = {}
    out: dict[int, OptionSide] = {}
    for _day, _rank, _id, ev in events:
        if isinstance(ev, OptionAdjustRow):
            _adjust(lots, ev)
        else:
            out[ev.id] = _classify(lots, ev)
    return out


def _classify(lots: dict[tuple[str, str], _Lots], r: OptionRow) -> OptionSide:
    buy = str(r.side).upper() == "BUY"
    qty = r.quantity
    key = (r.account_id, r.symbol)
    if r.close_reason is not None:
        label = _CLOSE_SIDE.get(r.close_reason, OptionSide.UNBOOKED)
        if qty <= _ZERO:
            return label
        lot = lots.get(key)
        room = (lot.short if buy else max(lot.long, _ZERO)) if lot is not None else _ZERO
        if qty > room:
            return OptionSide.UNBOOKED
        assert lot is not None
        if buy:
            lot.short -= qty
        else:
            lot.long -= qty
        return label
    if qty <= _ZERO:
        return OptionSide.BTO if buy else OptionSide.STO if r.short_sale else OptionSide.STC
    lot = lots.setdefault(key, _Lots())
    if buy:
        cover = min(qty, lot.short)
        lot.short -= cover
        lot.long += qty - cover
        if cover == _ZERO:
            return OptionSide.BTO
        return OptionSide.BTC if cover == qty else OptionSide.BTC_BTO
    if r.short_sale:
        from_long = min(qty, max(lot.long, _ZERO))
        to_short = qty - from_long
        lot.long -= from_long
        lot.short += to_short
        if from_long == _ZERO:
            return OptionSide.STO
        return OptionSide.STC if to_short == _ZERO else OptionSide.STC_STO
    if qty > lot.long:
        lot.oversold = True
        lot.long -= qty
        return OptionSide.STC_OVERSOLD
    lot.long -= qty
    return OptionSide.STC


def _adjust(lots: dict[tuple[str, str], _Lots], a: OptionAdjustRow) -> None:
    if a.from_symbol == a.to_symbol or not _is_positive_integer(a.ratio_to):
        return
    src = lots.get((a.account_id, a.from_symbol))
    if src is None or (src.long == _ZERO and src.short == _ZERO) or src.oversold:
        return
    dst_key = (a.account_id, a.to_symbol)
    dst = lots.get(dst_key)
    if dst is not None and (dst.oversold
                            or (src.long > _ZERO and dst.short > _ZERO)
                            or (src.short > _ZERO and dst.long > _ZERO)):
        return
    dst = lots.setdefault(dst_key, _Lots())
    dst.long += src.long * a.ratio_to
    dst.short += src.short * a.ratio_to
    src.long = _ZERO
    src.short = _ZERO
