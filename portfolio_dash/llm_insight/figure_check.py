"""Read-time figure check for a stored insight card (M9, owner audit 2026-09-16).

**What this is.** A PURE, read-time post-check that compares the figures a card PRINTS
against the numbers the model was FED — ``insights.prompt_figures``, every numeric token of
the exact prompt text that produced the card, recorded at generation time. It never
blocks, never rewrites and never hides a card; it returns two capped lists plus a
``snapshot`` state the API attaches to the card so the page can mark it 「數值待核」.

**Why the population is the prompt's numbers, not ``input_snapshot``** (re-verification
2026-09-17, the audit author's ❌ on M9). The first version compared against
``insights.input_snapshot``, documented here as "the exact JSON fed into the prompt". It
never was: ``generate.RunInputs.input_snapshots`` is a seam no caller feeds, so every stored
row holds the fallback fingerprint tag (``"2026-07-05|US"``, 13–20 characters) — measured
on the demo database, 149 of 149 cards, the newest from 2026-08-25. The unit tests passed
because they supplied a synthetic JSON snapshot; the check had never run against a real
card and ``unverified_figures`` had fired 0 times on 148 cards, the audit's ×1000 card
included. The population is now extracted from the prompt string at the single generation
site (:func:`prompt_figures_json`), stored in its own additive column, and read here.
``input_snapshot`` is untouched: it feeds the cache fingerprint and the Loop-2 master
prompt, and changing it would have moved cache semantics to fix a display flag.

**The third state.** A card generated before the column existed has no population, and
"cannot check" must not read as "clean" — the audit's #37 card carried no pill for exactly
that reason. ``snapshot == "none"`` means: this card prints at least one figure and there is
nothing to check it against. A legacy card that prints no figure at all is vacuously
``"checked"``, so the state appears exactly where it carries information.

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
* a parenthesised 4–6-digit code is read as a TICKER, not as a figure (it is the symbol
  check's business — and 「（00878）」 read as the number 878 would be a flag on every
  five-digit TW ETF), and common uppercase abbreviations (PE / ETF / USD …) plus every
  period-suffixed indicator (MA20 / MA200 / RSI14 …) are never read as tickers — measured
  2026-09-17: 47 of 48 ``unknown_symbols`` hits on the demo were PBR / MA20 / MA60 / MA120 /
  MA200 / MA50 / RSI14 / BUY / HOLD / KLCI / TAIEX, and one was the real 6883;
* a population with NO number at all yields NO figure flags — it yields the ``"none"``
  state instead, because an empty population is not evidence of a wrong number, and a
  silent ``[]`` is not evidence of a right one.

Pure: no connection, no clock, no LLM. ``llm_insight`` may import ``portfolio``/``shared``
only (architecture.md), and this module imports neither — it is a leaf.
"""

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

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

#: A parenthesised ticker: a 4–6-digit TW/MY-style code (0050 / 00878 / 006208 / 5225), or
#: an uppercase US-style symbol. Masked out of the number scan (a code is not a figure) and
#: fed to the symbol check.
_CODE_RE = re.compile(r"[(（]\s*(\d{4,6}|[A-Z][A-Z0-9]{0,5}(?:\.[A-Z]{1,3})?)\s*[)）]")

#: Uppercase tokens a card legitimately writes in parentheses that are NOT tickers. Without
#: this, 「本益比（PE）」 and 「單位：USD」 would each raise a hallucinated-symbol flag.
#: Grouped by what they are so the next addition lands in the right row; a registered
#: symbol is never flagged regardless of this set (the registry check runs first), so the
#: only cost of a name here is a missed flag on an unregistered code of the same spelling.
_NOT_A_TICKER = frozenset({
    # markets / currencies / units
    "AI", "LLM", "US", "TW", "MY", "KL", "OTC", "USD", "TWD", "MYR", "NTD", "RM",
    "CNY", "RMB", "JPY", "EUR", "GBP", "HKD", "SGD", "AUD",
    # instrument kinds / corporate terms
    "ETF", "ETN", "REIT", "ADR", "DRIP", "IPO", "ESG", "NAV", "TTM", "GAAP", "EBITDA",
    # valuation ratios and return measures
    "EPS", "DPS", "BPS", "SPS", "PE", "PER", "PB", "PBR", "PEG", "PS", "PSR", "PCF",
    "ROE", "ROA", "ROI", "ROIC", "EV", "FCF", "DCF", "WACC", "NPV", "YTM", "APY", "APR",
    "CAGR", "XIRR", "TWR", "IRR", "YOY", "QOQ", "MOM", "YTD", "MTD", "QTD",
    # macro / regulators / fee names
    "GDP", "CPI", "PPI", "PMI", "FED", "FOMC", "SEC", "TAF", "CAT", "SST", "GST",
    # bare indicators (period-suffixed forms are matched by _INDICATOR_RE below)
    "MA", "EMA", "SMA", "WMA", "RSI", "MACD", "KD", "KDJ", "ATR", "ADX", "CCI", "OBV",
    "DMI", "SAR", "MFI", "VWAP", "BOLL", "BB", "VIX", "BETA", "SD",
    # index names a card writes as 「（TAIEX）」 / 「（KLCI）」
    "TAIEX", "TWII", "TPEX", "KLCI", "SPX", "NDX", "DJI", "DJIA", "SOX",
    # ratings / stances
    "BUY", "SELL", "HOLD", "LONG", "SHORT", "BULL", "BEAR", "OW", "UW", "EW",
    # period / misc tokens
    "Q1", "Q2", "Q3", "Q4", "H1", "H2", "FY", "OK", "NA", "N", "A",
})

#: An indicator with its period — 「MA20 / MA200 / RSI14 / EMA12 / KD9」. A name-only set
#: cannot enumerate these (the period is free text), so the rule matches the shape: one of
#: the indicator stems above followed by digits. No real ticker on the three markets has
#: this shape — TW/MY codes are all-digit, US symbols are all-letter (plus a dotted class).
_INDICATOR_RE = re.compile(
    r"^(?:MA|EMA|SMA|WMA|DMA|RSI|KD|KDJ|ATR|ADX|CCI|OBV|DMI|SAR|MFI|ROC|MACD|BOLL|BB)\d+$"
)

#: A number, optionally grouped with thousands separators, plus an optional unit suffix.
#: The lookbehind keeps the scan off the tail of a longer token (a version string, an id).
_NUM_RE = re.compile(
    r"(?<![\d.A-Za-z])"
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(%|％|千|萬|億|[KkMm](?![A-Za-z0-9]))?"
)


SnapshotState = Literal["checked", "none"]


class FigureFlags(BaseModel):
    """The read-time check's verdict for ONE card.

    Both lists empty AND ``snapshot == "checked"`` = nothing to disclose. ``"none"`` = the
    card prints figures and no population was recorded to check them against (a card
    generated before ``prompt_figures`` existed) — disclosed as its own state, never as a
    clean ``[]``.
    """

    #: Figures printed by the card that match no fed number at any plausible scale.
    unverified_figures: list[str] = Field(default_factory=list)
    #: Parenthesised ticker-shaped codes in the card text that are not registered symbols.
    unknown_symbols: list[str] = Field(default_factory=list)
    #: Whether the figure comparison could run at all (see the class docstring).
    snapshot: SnapshotState = "checked"


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


def _card_figures(card_text: str, known_forms: set[str]) -> list[tuple[str, Decimal, bool]]:
    """Every figure the card prints: ``(token as printed, value at its printed scale,
    is_percent)``. Counts, years, dates and parenthesised codes are not figures — and
    neither is a registered all-digit symbol written bare (「2330 量縮整理」, the anomaly
    card's own 「2330 資料異常」 title): it is a name, and reading it as the number 2,330
    would demand that every population contain it."""
    figures: list[tuple[str, Decimal, bool]] = []
    for match in _NUM_RE.finditer(_mask(card_text)):
        raw, suffix = match.group(1), (match.group(2) or "")
        value = _decimal_or_none(raw)
        if value is None or _is_not_a_figure(raw, suffix, value):
            continue
        if not suffix and raw in known_forms:
            continue  # a bare registered code, not a figure
        scaled = value * _MULTIPLIERS.get(suffix, Decimal(1))
        figures.append((match.group(0).strip(), scaled, suffix in _PERCENT_SUFFIXES))
    return figures


def _unverified_figures(
    figures: list[tuple[str, Decimal, bool]], population: list[Decimal]
) -> list[str]:
    """The printed figures that match no fed number at any plausible scale (capped)."""
    flagged: list[str] = []
    for token, scaled, is_percent in figures:
        if any(
            _matches(candidate, known)
            for candidate in _candidates(scaled, is_percent=is_percent)
            for known in population
        ):
            continue
        if token not in flagged:
            flagged.append(token)
        if len(flagged) >= MAX_FLAGS:
            break
    return flagged


def prompt_figures_json(prompt: str) -> str:
    """The comparison population for a card, extracted from the prompt that produced it.

    Called ONCE at the generation site with the exact string handed to the model, and
    stored as ``insights.prompt_figures``. Every numeric token is kept at the scale the
    model saw it, de-duplicated, as a JSON list of strings: ``"4,290.80"`` → ``"4290.80"``,
    ``"12.49%"`` stays a percent form (the reader expands it to the ratio too), ``"120 億"``
    is stored scaled. Deliberately PERMISSIVE — years, dates and counts all stay in — because
    a population that is too small produces false 待核 pills, and the reader's own filters
    already keep those tokens off the card side. Nothing is capped: a truncated population
    would silently turn correct figures into flags.
    """
    seen: dict[str, None] = {}
    for match in _NUM_RE.finditer(prompt):
        raw, suffix = match.group(1), (match.group(2) or "")
        value = _decimal_or_none(raw)
        if value is None:
            continue
        if suffix in _PERCENT_SUFFIXES:
            seen[f"{value}%"] = None
        else:
            seen[str(value * _MULTIPLIERS.get(suffix, Decimal(1)))] = None
    return json.dumps(list(seen))


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
        if code in forms or code.split(".")[0] in forms:
            continue  # registered — never a hallucination, whatever it looks like
        if code in _NOT_A_TICKER or _INDICATOR_RE.match(code):
            continue  # an abbreviation / indicator the card wrote in parentheses
        if code not in flagged:
            flagged.append(code)
        if len(flagged) >= MAX_FLAGS:
            break
    return flagged


def check_figures(
    card_text: str, snapshot_json: str, known_symbols: set[str]
) -> FigureFlags:
    """Post-check ONE card's text against the numbers it was fed (pure; see module docstring).

    *card_text* is the card as the owner reads it (``title + summary + body_md``),
    *snapshot_json* the stored population — ``insights.prompt_figures`` (a JSON list from
    :func:`prompt_figures_json`) or any JSON whose numeric leaves are the fed values —
    *known_symbols* the registered instrument symbols. Returns the two capped lists and the
    ``snapshot`` state: ``"none"`` when the card prints a figure and the population holds
    no number (blank, not JSON, or JSON without a numeric leaf — the legacy
    ``"2026-07-05|US"`` fingerprint tag is all three at once).
    """
    figures = _card_figures(card_text, _known_forms(known_symbols))
    population = snapshot_numbers(snapshot_json)
    if not population:
        return FigureFlags(
            unknown_symbols=_unknown_symbols(card_text, known_symbols),
            snapshot="none" if figures else "checked",
        )
    return FigureFlags(
        unverified_figures=_unverified_figures(figures, population),
        unknown_symbols=_unknown_symbols(card_text, known_symbols),
    )
