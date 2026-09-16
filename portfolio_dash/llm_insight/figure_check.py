"""Read-time figure check for a stored insight card (M9, owner audit 2026-09-16).

**What this is.** A PURE, read-time post-check that compares the figures a card PRINTS
against the variable snapshot the card was GENERATED from (``insights.input_snapshot`` —
the exact JSON fed into the prompt). It never blocks, never rewrites and never hides a
card; it returns two lists the API attaches to the card so the page can mark it 「數值待核」.

**Why it exists.** Measured on cached cards: one card said 「未實現獲利 429.1 萬美元」 while
its sibling from the SAME batch said 「未實現收益 4,290.80 美元，部位規模共 11,951 美元成本」 —
a ×1000 scale error — and another named 「LRDIM (6883)」, a code that exists in neither the
holdings nor the watchlist. The system's own 「AI 戰績」 page measures a 13.33% quantitative
hit rate, so this is a known failure mode, not a one-off. ``llm-insight.md``'s rule is that
the LLM never emits numbers of record; this is the read-side check that the rule held.

**Why a post-check and not a gate.** Blocking would mean deleting the qualitative content
over a number the checker could not align — and the checker is deliberately the weaker
party here (see the conservatism rules below). Flagging matches the house posture for an
uncertain value everywhere else in this repo: disclosed, never guessed, never silently
dropped (a stale price is labelled; an unknown ETF flag raises a soft issue).

**Conservative by construction** — a false "待核" pill on a correct card costs the owner
trust in the flag, so every rule below errs towards NOT flagging:

* a figure is VERIFIED when ANY snapshot number matches it at ANY plausible scale, within
  0.5% relative or 0.005 absolute;
* percent forms additionally try ×100 / ÷100 (a snapshot stores ``0.1249`` for 「12.49%」);
* bare integers < 100 (counts: 5 天, 3 個帳戶), 4-digit years and date-like tokens are not
  figures and are skipped;
* a parenthesised 4-digit code is read as a TICKER, not as a figure (it is the symbol
  check's business), and common uppercase abbreviations (PE / ETF / USD …) are never read
  as tickers;
* a snapshot with NO parseable number at all yields NO figure flags — an empty snapshot is
  not evidence of a wrong number.

Pure: no connection, no clock, no LLM. ``llm_insight`` may import ``portfolio``/``shared``
only (architecture.md), and this module imports neither — it is a leaf.
"""

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field

#: Each list is capped so a badly-formatted card cannot produce an unreadable tooltip.
MAX_FLAGS = 5

#: Relative / absolute tolerance for "the card's figure IS this snapshot number".
#: 0.5% absorbs the rounding a prompt does on the way in (a 2-dp render of a 6-dp Decimal)
#: without absorbing a scale error — the audit's ×1000 is 5 orders of magnitude past it.
_REL_TOL = Decimal("0.005")
_ABS_TOL = Decimal("0.005")

#: Multipliers a card may apply to a figure. The zh ones are the whole point of M9: 「429.1
#: 萬」 IS 4,291,000 and must be compared as such, or the scale error reads as a match.
_MULTIPLIERS: dict[str, Decimal] = {
    "千": Decimal("1000"),
    "萬": Decimal("10000"),
    "億": Decimal("100000000"),
    "K": Decimal("1000"),
    "k": Decimal("1000"),
    "M": Decimal("1000000"),
    "m": Decimal("1000000"),
}
_PERCENT_SUFFIXES = ("%", "％")

#: Date-like tokens are masked out BEFORE the number scan: 2026-09-16 / 2026年9月16日 /
#: 9/16 are not figures, and their parts would otherwise be flagged as unverified numbers.
_DATE_RE = re.compile(
    r"\d{4}\s*[-/年]\s*\d{1,2}\s*(?:[-/月]\s*\d{1,2}\s*日?)?"
    r"|\d{1,2}\s*[/月]\s*\d{1,2}\s*日?"
)

#: A parenthesised ticker: a 4-digit TW/MY-style code, or an uppercase US-style symbol.
#: Masked out of the number scan (a code is not a figure) and fed to the symbol check.
_CODE_RE = re.compile(r"[(（]\s*(\d{4}|[A-Z][A-Z0-9]{0,5}(?:\.[A-Z]{1,3})?)\s*[)）]")

#: Uppercase tokens a card legitimately writes in parentheses that are NOT tickers. Without
#: this, 「本益比（PE）」 and 「單位：USD」 would each raise a hallucinated-symbol flag.
_NOT_A_TICKER = frozenset({
    "AI", "LLM", "US", "TW", "MY", "KL", "USD", "TWD", "MYR", "NTD", "RM",
    "ETF", "ETN", "REIT", "ADR", "DRIP", "IPO", "ESG", "NAV", "TTM", "GAAP", "EBITDA",
    "EPS", "PE", "PER", "PB", "PEG", "PS", "ROE", "ROA", "ROI", "EV", "FCF", "DCF",
    "CAGR", "XIRR", "TWR", "IRR", "YOY", "QOQ", "MOM", "YTD", "MTD", "QTD",
    "GDP", "CPI", "PPI", "PMI", "FED", "FOMC", "SEC", "TAF", "CAT", "SST", "GST",
    "MA", "EMA", "SMA", "RSI", "MACD", "KD", "ATR", "VIX", "BETA", "SD",
    "Q1", "Q2", "Q3", "Q4", "H1", "H2", "FY", "OK", "NA", "N", "A",
})

#: A number, optionally grouped with thousands separators, plus an optional unit suffix.
#: The lookbehind keeps the scan off the tail of a longer token (a version string, an id).
_NUM_RE = re.compile(
    r"(?<![\d.A-Za-z])"
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(%|％|千|萬|億|[KkMm](?![A-Za-z0-9]))?"
)


class FigureFlags(BaseModel):
    """The read-time check's verdict for ONE card. Both lists empty = nothing to flag."""

    #: Figures printed by the card that match no snapshot number at any plausible scale.
    unverified_figures: list[str] = Field(default_factory=list)
    #: Parenthesised ticker-shaped codes in the card text that are not registered symbols.
    unknown_symbols: list[str] = Field(default_factory=list)


def _decimal_or_none(text: str) -> Decimal | None:
    """Parse a numeric token (thousands separators allowed) as Decimal, else None."""
    try:
        return Decimal(text.replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _collect_numbers(node: Any, out: list[Decimal]) -> None:
    """Walk a decoded JSON tree and collect EVERY numeric leaf, numeric strings included.

    Numeric strings matter more than JSON numbers here: this repo puts money on the wire as
    Decimal STRINGS, so a snapshot's holdings block carries ``"4290.80"``, not ``4290.8``.
    ``bool`` is excluded explicitly — it is an ``int`` subclass in Python, and ``True``
    would otherwise enter the comparison population as the number 1.
    """
    if isinstance(node, bool):
        return
    if isinstance(node, int):
        out.append(Decimal(node))
        return
    if isinstance(node, float):
        # A float only ever reaches here from a provider-shaped JSON leaf; str() first so the
        # binary tail does not enter the comparison (the repo's float-noise discipline).
        out.append(Decimal(str(node)))
        return
    if isinstance(node, str):
        token = node.strip()
        pct = token.endswith(_PERCENT_SUFFIXES)
        value = _decimal_or_none(token[:-1] if pct else token)
        if value is not None:
            out.append(value)
            if pct:
                # A snapshot that itself writes "12.49%" also stands for the ratio 0.1249.
                out.append(value / Decimal("100"))
        return
    if isinstance(node, dict):
        for item in node.values():
            _collect_numbers(item, out)
        return
    if isinstance(node, list):
        for item in node:
            _collect_numbers(item, out)


def snapshot_numbers(snapshot_json: str) -> list[Decimal]:
    """Every numeric leaf in the stored input snapshot, de-duplicated.

    An unparseable / empty snapshot yields ``[]``, which the caller reads as "cannot check"
    — NOT as "every figure is wrong".
    """
    try:
        payload = json.loads(snapshot_json) if snapshot_json.strip() else None
    except (json.JSONDecodeError, ValueError):
        return []
    out: list[Decimal] = []
    _collect_numbers(payload, out)
    return list(dict.fromkeys(out))


def _matches(figure: Decimal, snapshot: Decimal) -> bool:
    """True when the two numbers have the same MAGNITUDE, within the tolerance.

    Compared on absolute value deliberately: this checker is about SCALE (the audit's
    ×1000) and invented numbers, not about sign. A card writing 「虧損 3,200 美元」 or
    「−12.5%」 against a snapshot holding ``-3200`` / ``-0.125`` is correct, and flagging it
    would be precisely the false positive that would teach the owner to ignore the pill.
    """
    lhs, rhs = abs(figure), abs(snapshot)
    diff = abs(lhs - rhs)
    if diff <= _ABS_TOL:
        return True
    scale = max(lhs, rhs)
    return scale > 0 and diff <= _REL_TOL * scale


def _candidates(value: Decimal, *, is_percent: bool) -> list[Decimal]:
    """The scales a printed figure may legitimately have been stored at.

    A percent form is the only ambiguous one: the snapshot may hold the ratio (0.1249) or
    the percentage (12.49). Everything else is compared at exactly the scale it was printed
    at — which is precisely what makes the audit's 「429.1 萬」 vs ``4290.80`` a flag.
    """
    if is_percent:
        return [value, value / Decimal("100"), value * Decimal("100")]
    return [value]


def _is_not_a_figure(raw: str, suffix: str, value: Decimal) -> bool:
    """True for tokens that are counts or years rather than figures (never flagged)."""
    if suffix or "." in raw or "," in raw:
        return False
    if value < 100:
        return True  # a count: 5 天 / 3 個帳戶 / 14 日
    return len(raw) == 4 and Decimal(1990) <= value <= Decimal(2099)  # a year


def _mask(text: str) -> str:
    """Blank out date-like tokens and parenthesised ticker codes before the number scan."""
    masked = _DATE_RE.sub(lambda m: " " * len(m.group(0)), text)
    return _CODE_RE.sub(lambda m: " " * len(m.group(0)), masked)


def _unverified_figures(card_text: str, snapshot: list[Decimal]) -> list[str]:
    """Figures in *card_text* that match no snapshot number at any plausible scale."""
    if not snapshot:
        return []  # nothing to compare against → nothing is "unverified"
    flagged: list[str] = []
    for match in _NUM_RE.finditer(_mask(card_text)):
        raw, suffix = match.group(1), (match.group(2) or "")
        value = _decimal_or_none(raw)
        if value is None or _is_not_a_figure(raw, suffix, value):
            continue
        is_percent = suffix in _PERCENT_SUFFIXES
        scaled = value * _MULTIPLIERS.get(suffix, Decimal(1))
        if any(
            _matches(candidate, known)
            for candidate in _candidates(scaled, is_percent=is_percent)
            for known in snapshot
        ):
            continue
        token = match.group(0).strip()
        if token not in flagged:
            flagged.append(token)
        if len(flagged) >= MAX_FLAGS:
            break
    return flagged


def _known_forms(known_symbols: set[str]) -> set[str]:
    """Known symbols, upper-cased, plus each one's pre-suffix base ("1155.KL" → "1155").

    A card writes 「（1155）」 for a symbol the ledger stores as ``1155.KL``; treating that as
    a hallucination would be exactly the false positive this module must not produce.
    """
    forms: set[str] = set()
    for symbol in known_symbols:
        upper = symbol.strip().upper()
        if not upper:
            continue
        forms.add(upper)
        forms.add(upper.split(".")[0])
    return forms


def _unknown_symbols(card_text: str, known_symbols: set[str]) -> list[str]:
    """Parenthesised ticker-shaped codes in the text that are not registered symbols."""
    forms = _known_forms(known_symbols)
    flagged: list[str] = []
    for match in _CODE_RE.finditer(card_text):
        code = match.group(1).upper()
        if code in _NOT_A_TICKER or code in forms or code.split(".")[0] in forms:
            continue
        if code not in flagged:
            flagged.append(code)
        if len(flagged) >= MAX_FLAGS:
            break
    return flagged


def check_figures(
    card_text: str, snapshot_json: str, known_symbols: set[str]
) -> FigureFlags:
    """Post-check ONE card's text against its own input snapshot (pure; see module docstring).

    *card_text* is the card as the owner reads it (``title + summary + body_md``),
    *snapshot_json* the stored ``insights.input_snapshot``, *known_symbols* the registered
    instrument symbols. Returns the two capped lists; both empty means nothing to disclose.
    """
    return FigureFlags(
        unverified_figures=_unverified_figures(card_text, snapshot_numbers(snapshot_json)),
        unknown_symbols=_unknown_symbols(card_text, known_symbols),
    )
