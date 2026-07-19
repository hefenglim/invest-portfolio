"""Canonical sector vocabulary + sector normalization (GICS 2023, R6 owner-signed).

PROBLEM (unchanged since FU-D31): instrument sectors are free text. Live data carries
``Tech`` vs ``Technology`` as two separate donut slices, EN/zh synonyms coexist
(``Financials`` / ``金融``), and TW stocks are often blank. This poisons BOTH the
sector-allocation donut AND the ``sector_weight`` concentration alert — which group on the
SAME aggregated ``SectorAllocation`` (one seam), so a single canonicalization fixes both.

R6 DECISION (owner sign-off 2026-07-19) — supersedes FU-D31's 15-key list:
adopt **GICS 2023 — 11 sectors** as the canonical vocabulary. Two former standalone keys
FOLD IN:
  * **Semiconductors → Information Technology** (GICS places semiconductors inside IT;
    the owner no longer wants TSMC/NVDA split from generic tech).
  * **Shipping → Industrials** (GICS classifies marine transportation under Industrials;
    TW 航運 stops being its own slice).
The dashboard sector donut and the ``sector_weight`` alert regroup accordingly.

**ETF stays a special NON-GICS bucket:** GICS classifies companies, not funds, so an ETF
has no GICS sector; the app keeps ``ETF`` as its own category (and the ``is_etf`` tax flag
is the source of truth for fee/stamp treatment — never derived from this label).

STORAGE (R6, refines FU-D31): stored instrument sectors are now **actually migrated** — a
one-time idempotent rewrite through :func:`canonical_sector` at the schema/boot seam
(``data_ingestion.store.migrate_instrument_sectors``, called from
``data_ingestion.schema.create_tables``). Read-time canonicalization at the donut grouping
seam (``portfolio/dashboard.py``) is KEPT as defense-in-depth so a provider- or CSV-supplied
synonym still groups correctly before the next boot migrates it.

DESIGN RULING (unchanged from FU-D31): canonical vocabulary KEYS are ENGLISH (stable,
matches provider data such as yfinance sector names). The dropdown OPTION LABELS may show
dual text 「Information Technology（資訊科技）」 for usability, but STORED VALUES / DONUT
LABELS / ALERT TEXT stay canonical English; zh display everywhere is deferred to the server
``display_name`` phase. The ``zh`` field below feeds ONLY the dropdown label, never storage
or grouping.

DATA INTEGRITY (unchanged): an unrecognized NON-EMPTY sector passes through UNCHANGED — it
is NEVER silently rebucketed. Only labels we positively know are synonyms merge.

PURITY: no money, no I/O, no imports of internal layers. A sector label is a category,
never a number of record (invariant #1 is not touched by labeling).
"""

from typing import TypedDict


class CanonicalSector(TypedDict):
    """One vocabulary row: a stable ENGLISH ``key`` (stored + grouped value) and its
    Traditional-Chinese ``zh`` display label (dropdown option text only)."""

    key: str
    zh: str


# The canonical vocabulary — GICS 2023's 11 sectors + the non-GICS ``ETF`` bucket + a
# catch-all. Order = dropdown order; ``Unclassified`` (the blank/None bucket) is always LAST.
# The 11 GICS keys (all but ETF / Unclassified) are also exported as ``GICS_SECTOR_KEYS`` for
# the next wave's AI sector prompt.
CANONICAL_SECTORS: list[CanonicalSector] = [
    {"key": "Information Technology", "zh": "資訊科技"},
    {"key": "Communication Services", "zh": "通訊服務"},
    {"key": "Financials", "zh": "金融"},
    {"key": "Health Care", "zh": "醫療保健"},
    {"key": "Consumer Discretionary", "zh": "非必需消費"},
    {"key": "Consumer Staples", "zh": "必需消費"},
    {"key": "Industrials", "zh": "工業"},
    {"key": "Energy", "zh": "能源"},
    {"key": "Materials", "zh": "原物料"},
    {"key": "Utilities", "zh": "公用事業"},
    {"key": "Real Estate", "zh": "房地產"},
    {"key": "ETF", "zh": "ETF"},
    {"key": "Unclassified", "zh": "未分類"},
]

UNCLASSIFIED = "Unclassified"

# The set of canonical keys — the membership test the API uses to validate an AI-returned
# sector (POST /api/instruments/ai-resolve downgrades off-vocabulary sectors), and the
# guard the migration uses before rewriting a stored value.
CANONICAL_KEYS: frozenset[str] = frozenset(s["key"] for s in CANONICAL_SECTORS)

# The 11 GICS sector keys in dropdown order, EXCLUDING the non-GICS ``ETF`` bucket and the
# ``Unclassified`` catch-all. The next wave embeds this in the AI sector-detection prompt so
# the model only proposes real GICS sectors. Derived from CANONICAL_SECTORS to avoid drift.
GICS_SECTOR_KEYS: tuple[str, ...] = tuple(
    s["key"] for s in CANONICAL_SECTORS if s["key"] not in {"ETF", UNCLASSIFIED}
)


# Synonym → canonical key. KEYS ARE case-folded (compared against ``raw.strip().casefold()``).
# Covers EN variants, common abbreviations, provider (yfinance) sector names, zh-TW labels,
# AND the OLD canonical keys (``Semiconductors`` / ``Shipping`` / ``Technology`` / ``Healthcare``)
# so BOTH the one-time migration and read-time grouping catch legacy stored values. Every
# canonical key maps to ITSELF here so exact canonical input (any case) is stable. Anything NOT
# listed passes through UNCHANGED — an unrecognized non-empty sector is NEVER silently
# rebucketed (we only merge labels we positively know are synonyms).
_SYNONYMS: dict[str, str] = {
    # Information Technology (GICS) — folds in the former Technology + Semiconductors keys.
    "information technology": "Information Technology",
    "technology": "Information Technology",
    "tech": "Information Technology",
    "infotech": "Information Technology",
    "科技": "Information Technology",
    "資訊科技": "Information Technology",
    "資訊技術": "Information Technology",
    # Semiconductors (R6: FOLDED into Information Technology — no longer its own key).
    "semiconductors": "Information Technology",
    "semiconductor": "Information Technology",
    "semis": "Information Technology",
    "semi": "Information Technology",
    "半導體": "Information Technology",
    # Communication Services (GICS) — telecom family.
    "communication services": "Communication Services",
    "communications": "Communication Services",
    "communication": "Communication Services",
    "telecom": "Communication Services",
    "telecommunications": "Communication Services",
    "通訊服務": "Communication Services",
    "通訊": "Communication Services",
    "電信": "Communication Services",
    # Financials (GICS) — banking family.
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
    # Health Care (GICS) — note the space; the old ``Healthcare`` key migrates here.
    "health care": "Health Care",
    "healthcare": "Health Care",
    "醫療保健": "Health Care",
    "醫療": "Health Care",
    "生技醫療": "Health Care",
    # Consumer Discretionary (GICS).
    "consumer discretionary": "Consumer Discretionary",
    "consumer cyclical": "Consumer Discretionary",
    "非必需消費": "Consumer Discretionary",
    "非必需性消費": "Consumer Discretionary",
    # Consumer Staples (GICS).
    "consumer staples": "Consumer Staples",
    "consumer defensive": "Consumer Staples",
    "必需消費": "Consumer Staples",
    "必需性消費": "Consumer Staples",
    # Industrials (GICS) — folds in the former Shipping key.
    "industrials": "Industrials",
    "industrial": "Industrials",
    "工業": "Industrials",
    "shipping": "Industrials",
    "marine": "Industrials",
    "航運": "Industrials",
    # Energy (GICS).
    "energy": "Energy",
    "能源": "Energy",
    # Materials (GICS).
    "materials": "Materials",
    "basic materials": "Materials",
    "原物料": "Materials",
    "原材料": "Materials",
    # Utilities (GICS).
    "utilities": "Utilities",
    "utility": "Utilities",
    "公用事業": "Utilities",
    # Real Estate (GICS).
    "real estate": "Real Estate",
    "reit": "Real Estate",
    "reits": "Real Estate",
    "房地產": "Real Estate",
    "不動產": "Real Estate",
    # ETF (non-GICS fund bucket the app treats as a sector category).
    "etf": "ETF",
    # Unclassified (explicit catch-all synonyms).
    "unclassified": "Unclassified",
    "未分類": "Unclassified",
    "其他": "Unclassified",
    "other": "Unclassified",
}


def canonical_sector(raw: str | None) -> str:
    """Map a free-text sector label to its canonical ENGLISH GICS key.

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
