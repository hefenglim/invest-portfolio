"""Data-source management persistence: keys, health, and per-account fallback chains.

Spec 14.0 introduces three ``config_store`` tables (category ``"data_sources"``):

- ``data_sources``           — API key + enabled flag, keyed by source id.
- ``data_source_health``     — last connection-test status / latency / detail.
- ``data_source_fallbacks``  — per-account quote-source fallback chain (JSON array).

Static source *descriptions* (name / type / markets / auth / note) are a Python
constant table here — they are config-as-code, not user data, so they never hit
the DB (spec 14.0). The fallback chains are seeded from ``pricing/defaults.py``'s
current ``DEFAULT_PROVIDER_ORDER`` so this layer is the single source of truth once
seeded; an empty table falls back to the hardcoded default (see ``account_chains``).
"""

import json
import sqlite3

from pydantic import BaseModel

from portfolio_dash.pricing.defaults import DEFAULT_PROVIDER_ORDER
from portfolio_dash.pricing.enums import DataType
from portfolio_dash.shared import config_store
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.masking import mask_secret

CATEGORY = "data_sources"

# Token tier ranks (spec 20.15.2). Higher rank = more entitlements. A null/unset
# source tier is treated as the lowest effective tier ("free") for tier-gating.
TIER_ORDER: dict[str, int] = {"free": 0, "backer": 1, "sponsor": 2, "sponsorpro": 3}


# --- Static source descriptions (config-as-code; never persisted) -------------


class SourceInfo(BaseModel, frozen=True):
    """Static, human-facing description of a data source (not stored in the DB)."""

    id: str
    name: str
    type: str  # "stock" | "dividend" | "fx" | "news" (frontend groups on this)
    markets: list[str]
    auth: str  # "none" | "apikey" | "oauth"
    note: str
    provides: list[str] = []  # data types this source can supply (spec 20.1)
    status: str = "live"  # "live" | "pending" | "blocked" (spec 20.1)
    tiers: list[str] | None = None  # selectable token tiers (spec 20.15.2); None = N/A


# Ordered for stable GET output. ``type`` matches the frontend's grouping keys
# (settings-datasources.js): stock / dividend / fx / news. ``provides``/``status``
# (spec 20.1) carry the full per-source data-type catalog + readiness.
SOURCE_INFO: tuple[SourceInfo, ...] = (
    # --- live (implemented + validated) -----------------------------------
    SourceInfo(id="yfinance", name="Yahoo Finance", type="stock",
               markets=["US", "TW", "MY", "FX"], auth="none",
               provides=["quote_latest", "quote_history", "dividend", "fx",
                         "index", "sentiment"],
               note="美股、台股、馬股、ETF、匯率、指數、VIX・免金鑰（有速率限制）"),
    SourceInfo(id="twse", name="台灣證券交易所 (TWSE)", type="stock", markets=["TW"],
               auth="none", provides=["quote_latest"], note="台股收盤報價・免金鑰"),
    SourceInfo(id="tpex", name="櫃買中心 (TPEx)", type="stock", markets=["TW"],
               auth="none", provides=["quote_latest"], note="上櫃股票報價・免金鑰"),
    SourceInfo(id="finmind", name="FinMind", type="dividend", markets=["TW", "US", "FX"],
               auth="apikey",
               provides=["dividend", "quote_history", "institutional", "margin",
                         "valuation", "monthly_revenue", "financials", "news", "macro"],
               tiers=["free", "backer", "sponsor", "sponsorpro"],
               note="台股股利、籌碼、基本面・Free 層 600/hr"),
    SourceInfo(id="twstock", name="twstock", type="stock", markets=["TW"], auth="none",
               provides=["quote_latest"], note="台股盤中即時報價後備・免金鑰"),
    SourceInfo(id="stockprices_dev", name="stockprices.dev", type="stock", markets=["US"],
               auth="none", provides=["quote_latest"],
               note="美股報價後備・免金鑰（flaky，僅後備）"),
    SourceInfo(id="klsescreener", name="KLSE Screener", type="stock", markets=["MY"],
               auth="none", provides=["quote_latest"],
               note="馬股報價後備・3-dp string・免金鑰"),
    SourceInfo(id="malaysiastock", name="Malaysiastock.biz", type="stock", markets=["MY"],
               auth="none", provides=["quote_latest"],
               note="馬股次要 string 報價源・免金鑰"),
    SourceInfo(id="cnn_fng", name="CNN Fear & Greed", type="sentiment", markets=["ALL"],
               auth="none", provides=["sentiment"], note="市場情緒指數・免金鑰"),
    # --- pending (implemented; awaiting a key to validate) -----------------
    SourceInfo(id="fx_ecb", name="ECB 歐洲央行匯率", type="fx", markets=["ALL"],
               auth="none", provides=["fx"], status="pending",
               note="每日匯率・免金鑰（adapter 待接上，尚無連線測試）"),
    SourceInfo(id="alphavantage", name="Alpha Vantage", type="stock", markets=["US", "FX"],
               auth="apikey", provides=["quote_latest", "quote_history", "fx"],
               tiers=["free", "premium"], status="pending",
               note="美股後備來源・免費層約 25/day（待測試）"),
    SourceInfo(id="finnhub", name="Finnhub", type="stock", markets=["US"], auth="apikey",
               provides=["quote_latest", "dividend"], status="pending",
               note="美股即時報價 / 股利・60/min（待測試）"),
    SourceInfo(id="fred", name="FRED（聯準會經濟資料）", type="macro", markets=["ALL"],
               auth="apikey", provides=["macro"], status="pending",
               note="總體經濟序列・需金鑰（待測試）"),
    SourceInfo(id="schwab", name="Charles Schwab API", type="stock", markets=["US"],
               auth="oauth",
               provides=["quote_latest", "quote_history", "dividend", "positions"],
               status="pending", note="券商 API・OAuth・待申請（待測試）"),
    SourceInfo(id="pytrends", name="Google Trends", type="trends", markets=["ALL"],
               auth="none", provides=["trends"], status="pending",
               note="搜尋熱度敘事訊號・非官方易限流（待測試）"),
    SourceInfo(id="divtracker", name="Dividend Tracker API", type="dividend",
               markets=["US"], auth="apikey", provides=["dividend"], status="pending",
               note="美股股利資料（待測試）"),
    SourceInfo(id="newsapi", name="NewsAPI.org", type="news", markets=["ALL"],
               auth="apikey", provides=["news"], status="pending",
               note="財經新聞截取（待測試）"),
    # --- blocked (catalogue only) -----------------------------------------
    SourceInfo(id="bursa", name="Bursa Malaysia 官網", type="stock", markets=["MY"],
               auth="none", provides=["quote_latest"], status="blocked",
               note="Cloudflare JS challenge・需 headless（受阻）"),
)

SOURCE_INFO_BY_ID: dict[str, SourceInfo] = {s.id: s for s in SOURCE_INFO}
KNOWN_SOURCE_IDS: frozenset[str] = frozenset(SOURCE_INFO_BY_ID)


def source_ids() -> list[str]:
    """All known data-source ids, in display order."""
    return [s.id for s in SOURCE_INFO]


# --- Schema / seed ------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS data_sources (
    id TEXT PRIMARY KEY,
    api_key TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    tier TEXT
);
CREATE TABLE IF NOT EXISTS data_source_health (
    source_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    last_test TEXT,
    latency_ms INTEGER,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS data_source_fallbacks (
    account_id TEXT PRIMARY KEY,
    chain TEXT NOT NULL
);
"""


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> None:
    """Add ``column`` to ``table`` if absent (additive, idempotent migration).

    A LOCAL copy of the additive-migration pattern (see ``scheduler/jobs.py``),
    intentionally not imported so ``pricing/`` gains no upward dependency. ``PRAGMA
    table_info`` row index 1 is the column name (row_factory-agnostic).
    """
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def create_tables(conn: sqlite3.Connection) -> None:
    """Create the three data-source tables idempotently (safe on every startup).

    Applies the additive §20.15.2 ``tier`` migration so a legacy ``data_sources``
    table that predates the column still opens (the CREATE only adds it on a fresh DB).
    """
    conn.executescript(_DDL)
    _add_column_if_missing(conn, "data_sources", "tier", "TEXT")
    conn.commit()


def _default_quote_chain(market: Market) -> list[str]:
    """The default QUOTE_LATEST provider order for a market (from pricing/defaults)."""
    return list(DEFAULT_PROVIDER_ORDER.get((DataType.QUOTE_LATEST, market), []))


# Each account's fallback chain seeds from its market's default quote order. This
# local map is the account source for seeding (one entry per default account id), so
# pricing/ needs no import from data_ingestion (architecture.md: sibling lower layers
# must not import each other). Keep in sync with data_ingestion's DEFAULT_ACCOUNTS.
_ACCOUNT_MARKET: dict[str, Market] = {
    "tw_broker": Market.TW,
    "schwab": Market.US,
    "moomoo_my_us": Market.US,
    "moomoo_my_my": Market.MY,
}


def seed(conn: sqlite3.Connection) -> None:
    """Seed one row per known source and per account fallback chain. Idempotent.

    - ``data_sources``: a row per source id with no key, enabled.
    - ``data_source_health``: a row per source id, status ``"unknown"``.
    - ``data_source_fallbacks``: per-account chain from the market's default order.
    """
    for sid in source_ids():
        conn.execute(
            "INSERT INTO data_sources (id, api_key, enabled) VALUES (?, NULL, 1) "
            "ON CONFLICT(id) DO NOTHING",
            (sid,),
        )
        conn.execute(
            "INSERT INTO data_source_health (source_id, status, last_test, "
            "latency_ms, detail) VALUES (?, 'unknown', NULL, NULL, NULL) "
            "ON CONFLICT(source_id) DO NOTHING",
            (sid,),
        )
    for account_id, market in _ACCOUNT_MARKET.items():
        chain = _default_quote_chain(market)
        conn.execute(
            "INSERT INTO data_source_fallbacks (account_id, chain) VALUES (?, ?) "
            "ON CONFLICT(account_id) DO NOTHING",
            (account_id, json.dumps(chain)),
        )
    conn.commit()


def ensure_seeded(conn: sqlite3.Connection) -> None:
    """Create the data-source tables (always) and seed defaults (once)."""
    config_store.ensure_seeded(conn, CATEGORY, create=create_tables, seed=seed)


# --- Reads --------------------------------------------------------------------


class SourceState(BaseModel):
    """The persisted, mutable state for a single source (key + enabled + health)."""

    id: str
    api_key: str | None
    enabled: bool
    status: str
    last_test: str | None
    latency_ms: int | None
    detail: str | None
    tier: str | None = None  # user-marked token tier (spec 20.15.2); None = unset


def _row_to_state(row: sqlite3.Row) -> SourceState:
    return SourceState(
        id=row["id"],
        api_key=row["api_key"],
        enabled=bool(row["enabled"]),
        status=row["status"] if row["status"] is not None else "unknown",
        last_test=row["last_test"],
        latency_ms=row["latency_ms"],
        detail=row["detail"],
        tier=row["tier"],
    )


def get_state(conn: sqlite3.Connection, source_id: str) -> SourceState | None:
    """Return the persisted state for one source, or None if it has no row."""
    row = conn.execute(
        "SELECT s.id AS id, s.api_key AS api_key, s.enabled AS enabled, s.tier AS tier, "
        "       COALESCE(h.status, 'unknown') AS status, h.last_test AS last_test, "
        "       h.latency_ms AS latency_ms, h.detail AS detail "
        "FROM data_sources s LEFT JOIN data_source_health h ON h.source_id = s.id "
        "WHERE s.id = ?",
        (source_id,),
    ).fetchone()
    return _row_to_state(row) if row is not None else None


def list_states(conn: sqlite3.Connection) -> dict[str, SourceState]:
    """Return persisted state for every source row, keyed by id."""
    rows = conn.execute(
        "SELECT s.id AS id, s.api_key AS api_key, s.enabled AS enabled, s.tier AS tier, "
        "       COALESCE(h.status, 'unknown') AS status, h.last_test AS last_test, "
        "       h.latency_ms AS latency_ms, h.detail AS detail "
        "FROM data_sources s LEFT JOIN data_source_health h ON h.source_id = s.id"
    ).fetchall()
    return {r["id"]: _row_to_state(r) for r in rows}


def get_api_key(conn: sqlite3.Connection, source_id: str) -> str | None:
    """Read a source's plaintext API key from the DB (None if unset/unknown).

    This is the single token-getter the providers read through (spec 14.2), so the
    FinMind/Alpha-Vantage token lives in the DB, not an env var or constructor arg.
    """
    row = conn.execute(
        "SELECT api_key FROM data_sources WHERE id = ?", (source_id,)
    ).fetchone()
    if row is None:
        return None
    key = row["api_key"]
    return key if key else None


def account_chains(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Return per-account fallback chains; empty table -> hardcoded market defaults."""
    rows = conn.execute(
        "SELECT account_id, chain FROM data_source_fallbacks"
    ).fetchall()
    if rows:
        out: dict[str, list[str]] = {}
        for r in rows:
            parsed = json.loads(r["chain"])
            out[r["account_id"]] = [str(x) for x in parsed]
        return out
    # No rows persisted yet: fall back to the hardcoded default per account market.
    return {
        account_id: _default_quote_chain(market)
        for account_id, market in _ACCOUNT_MARKET.items()
    }


# --- Writes -------------------------------------------------------------------


def set_api_key(conn: sqlite3.Connection, source_id: str, api_key: str | None) -> None:
    """Set or clear a source's API key and reset its health to ``unknown`` (spec 14.2).

    Empty string clears the key (stored as NULL). The source row is upserted so a
    not-yet-seeded source still records the key.
    """
    stored = api_key if api_key else None
    conn.execute(
        "INSERT INTO data_sources (id, api_key, enabled) VALUES (?, ?, 1) "
        "ON CONFLICT(id) DO UPDATE SET api_key = excluded.api_key",
        (source_id, stored),
    )
    conn.execute(
        "INSERT INTO data_source_health (source_id, status, last_test, latency_ms, "
        "detail) VALUES (?, 'unknown', NULL, NULL, NULL) "
        "ON CONFLICT(source_id) DO UPDATE SET status = 'unknown', last_test = NULL, "
        "latency_ms = NULL, detail = NULL",
        (source_id,),
    )
    conn.commit()


def set_tier(conn: sqlite3.Connection, source_id: str, tier: str | None) -> None:
    """Set or clear a source's marked token tier (spec 20.15.2). Upserts the row.

    ``None`` clears the marking (stored as NULL → treated as the lowest effective tier
    when gating). Validation of the tier string against the source's selectable
    ``tiers`` is the router's concern; this is the raw persistence write.
    """
    conn.execute(
        "INSERT INTO data_sources (id, api_key, enabled, tier) VALUES (?, NULL, 1, ?) "
        "ON CONFLICT(id) DO UPDATE SET tier = excluded.tier",
        (source_id, tier),
    )
    conn.commit()


def get_tier(conn: sqlite3.Connection, source_id: str) -> str | None:
    """Read a source's marked token tier (None if unset/unknown). The single getter the
    tier-gating preflight reads through (spec 20.15.4)."""
    row = conn.execute(
        "SELECT tier FROM data_sources WHERE id = ?", (source_id,)
    ).fetchone()
    if row is None:
        return None
    tier = row["tier"]
    return tier if tier else None


def upsert_health(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    status: str,
    last_test: str | None,
    latency_ms: int | None,
    detail: str | None,
) -> None:
    """Record a connection-test result into ``data_source_health`` (idempotent upsert)."""
    conn.execute(
        "INSERT INTO data_source_health (source_id, status, last_test, latency_ms, "
        "detail) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(source_id) DO UPDATE SET status = excluded.status, "
        "last_test = excluded.last_test, latency_ms = excluded.latency_ms, "
        "detail = excluded.detail",
        (source_id, status, last_test, latency_ms, detail),
    )
    conn.commit()


def set_account_chains(
    conn: sqlite3.Connection, chains: dict[str, list[str]]
) -> None:
    """Overwrite the per-account fallback chains for the given accounts (idempotent)."""
    for account_id, chain in chains.items():
        conn.execute(
            "INSERT INTO data_source_fallbacks (account_id, chain) VALUES (?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET chain = excluded.chain",
            (account_id, json.dumps(chain)),
        )
    conn.commit()


def mask_token(api_key: str | None) -> str | None:
    """Mask a key for display (spec 14.1). Thin wrapper over the shared masker.

    Delegates to ``shared.masking.mask_secret`` so masking is defined once (review
    I-2): includes the short-key guard the old local implementation lacked.
    """
    return mask_secret(api_key)
