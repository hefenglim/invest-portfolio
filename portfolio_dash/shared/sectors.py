"""Canonical sector vocabulary + read-time normalization (FU-D31, P1①).

PROBLEM: instrument sectors are free text. Live data carries ``Tech`` vs ``Technology``
as two separate donut slices, EN/zh synonyms coexist (``Financials`` / ``金融``), and TW
stocks are often blank. This poisons BOTH the sector-allocation donut AND the
``sector_weight`` concentration alert — which group on the SAME aggregated
``SectorAllocation`` (one seam), so a single read-time canonicalization fixes both.

FIX: a curated, GICS-flavored canonical vocabulary of stable ENGLISH keys + a
synonym → canonical map applied at READ TIME (the dashboard's sector-allocation grouping
seam, ``portfolio/dashboard.py``). Stored instrument rows are NOT migrated this round —
canonicalization happens on read only.

DESIGN RULING (orchestrator, refines the mini-spec parenthetical): canonical vocabulary
KEYS are ENGLISH (stable, matches provider data such as yfinance sector names). The
dropdown OPTION LABELS may show dual text 「Technology（科技）」 for usability, but STORED
VALUES / DONUT LABELS / ALERT TEXT stay canonical English this round; zh display
everywhere is deferred to the server ``display_name`` phase. The ``zh`` field below feeds
ONLY the dropdown label, never storage or grouping.

PURITY: no money, no I/O, no imports of internal layers. A sector label is a category,
never a number of record (invariant #1 is not touched by labeling).
"""

from typing import TypedDict


class CanonicalSector(TypedDict):
    """One vocabulary row: a stable ENGLISH ``key`` (stored + grouped value) and its
    Traditional-Chinese ``zh`` display label (dropdown option text only)."""

    key: str
    zh: str


# The canonical vocabulary — a curated GICS-flavored list covering every sector literal
# present in the app (golden fixtures: Semiconductors / ETF / Shipping / Tech / Financials;
# demo seed: + Banking) plus the GICS sectors and a catch-all. Ordered for the dropdown;
# ``Unclassified`` (the blank/None bucket) is always LAST.
#
# ``Semiconductors`` and ``Shipping`` are kept as their OWN keys (not folded into
# Technology / Industrials): they are deliberate, distinct categories the owner uses
# (TSMC/NVDA are tagged "Semiconductors" apart from generic "Tech"; TW 航運 is its own
# slice in the golden). Folding them would silently merge meaningful slices AND could
# manufacture a spurious concentration alert — so they stay separate.
CANONICAL_SECTORS: list[CanonicalSector] = [
    {"key": "Technology", "zh": "科技"},
    {"key": "Semiconductors", "zh": "半導體"},
    {"key": "Communication Services", "zh": "通訊服務"},
    {"key": "Financials", "zh": "金融"},
    {"key": "Healthcare", "zh": "醫療保健"},
    {"key": "Consumer Discretionary", "zh": "非必需消費"},
    {"key": "Consumer Staples", "zh": "必需消費"},
    {"key": "Industrials", "zh": "工業"},
    {"key": "Shipping", "zh": "航運"},
    {"key": "Energy", "zh": "能源"},
    {"key": "Materials", "zh": "原物料"},
    {"key": "Utilities", "zh": "公用事業"},
    {"key": "Real Estate", "zh": "房地產"},
    {"key": "ETF", "zh": "ETF"},
    {"key": "Unclassified", "zh": "未分類"},
]

UNCLASSIFIED = "Unclassified"

# The set of canonical keys — the membership test the API uses to decide whether a mapped
# value is a real dropdown category (``mapped`` in POST /api/instruments/ai-sector).
CANONICAL_KEYS: frozenset[str] = frozenset(s["key"] for s in CANONICAL_SECTORS)


# Synonym → canonical key. KEYS ARE case-folded (compared against ``raw.strip().casefold()``).
# Covers EN variants, common abbreviations, provider (yfinance) sector names, and zh-TW
# labels. Every canonical key maps to ITSELF here so exact canonical input (any case) is
# stable. Anything NOT listed passes through UNCHANGED — an unrecognized non-empty sector is
# NEVER silently rebucketed (we only merge labels we positively know are synonyms).
_SYNONYMS: dict[str, str] = {
    # Technology
    "technology": "Technology",
    "tech": "Technology",
    "information technology": "Technology",
    "infotech": "Technology",
    "資訊科技": "Technology",
    "資訊技術": "Technology",
    "科技": "Technology",
    # Semiconductors (kept SEPARATE from Technology on purpose — see CANONICAL_SECTORS)
    "semiconductors": "Semiconductors",
    "semiconductor": "Semiconductors",
    "semis": "Semiconductors",
    "semi": "Semiconductors",
    "半導體": "Semiconductors",
    # Communication Services
    "communication services": "Communication Services",
    "communications": "Communication Services",
    "communication": "Communication Services",
    "telecom": "Communication Services",
    "telecommunications": "Communication Services",
    "通訊服務": "Communication Services",
    "通訊": "Communication Services",
    "電信": "Communication Services",
    # Financials
    "financials": "Financials",
    "financial": "Financials",
    "financial services": "Financials",
    "finance": "Financials",
    "banking": "Financials",
    "banks": "Financials",
    "bank": "Financials",
    "金融": "Financials",
    "金融服務": "Financials",
    "銀行": "Financials",
    # Healthcare
    "healthcare": "Healthcare",
    "health care": "Healthcare",
    "醫療保健": "Healthcare",
    "醫療": "Healthcare",
    "生技醫療": "Healthcare",
    # Consumer Discretionary
    "consumer discretionary": "Consumer Discretionary",
    "consumer cyclical": "Consumer Discretionary",
    "非必需消費": "Consumer Discretionary",
    "非必需性消費": "Consumer Discretionary",
    # Consumer Staples
    "consumer staples": "Consumer Staples",
    "consumer defensive": "Consumer Staples",
    "必需消費": "Consumer Staples",
    "必需性消費": "Consumer Staples",
    # Industrials
    "industrials": "Industrials",
    "industrial": "Industrials",
    "工業": "Industrials",
    # Shipping (a TW-popular category present in the golden; its own slice)
    "shipping": "Shipping",
    "marine": "Shipping",
    "航運": "Shipping",
    # Energy
    "energy": "Energy",
    "能源": "Energy",
    # Materials
    "materials": "Materials",
    "basic materials": "Materials",
    "原物料": "Materials",
    "原材料": "Materials",
    # Utilities
    "utilities": "Utilities",
    "utility": "Utilities",
    "公用事業": "Utilities",
    # Real Estate
    "real estate": "Real Estate",
    "reit": "Real Estate",
    "reits": "Real Estate",
    "房地產": "Real Estate",
    "不動產": "Real Estate",
    # ETF (a fund type the app treats as a sector bucket; present in golden + demo seed)
    "etf": "ETF",
    # Unclassified (explicit catch-all synonyms)
    "unclassified": "Unclassified",
    "未分類": "Unclassified",
    "其他": "Unclassified",
    "other": "Unclassified",
}


def canonical_sector(raw: str | None) -> str:
    """Map a free-text sector label to its canonical ENGLISH key (read-time only).

    Rules, in order:
      1. ``None`` or blank (after strip) → ``"Unclassified"``.
      2. A known synonym (whitespace-trimmed, case-insensitive) → its canonical key.
      3. Otherwise the ORIGINAL trimmed value, UNCHANGED — an unrecognized non-empty
         sector is NEVER silently rebucketed (data integrity: only known synonyms merge;
         everything else survives as its own slice).
    """
    if raw is None:
        return UNCLASSIFIED
    trimmed = raw.strip()
    if not trimmed:
        return UNCLASSIFIED
    return _SYNONYMS.get(trimmed.casefold(), trimmed)
