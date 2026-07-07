"""Separate-SQLite news store: organized news + a mentions index for per-symbol query.

A distinct database file (``news.db`` beside the ledger DB) — decision 2026-07-06: the
article text volume is larger than the ledger, keeping it in its own file avoids bloating
the transactional DB and leaves a clean seam for a future multi-account shared news DB.
Only AI-organized SUMMARIES are stored (2–4 sentences) plus the source link, never full
article bodies (token / storage / usage discipline).

Layering: depends only on ``shared`` (config) + stdlib. The api layer reads this store and
feeds ``symbol_news_json`` into a VarContext — ``llm_insight`` never imports it.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from portfolio_dash.shared.config import get_settings


def news_db_path() -> Path:
    """The news DB file: ``news.db`` in the same folder as the configured ledger DB.

    Deriving from ``db_path.parent`` means the two-environment isolation (prod vs demo
    data folders) applies automatically, and a future multi-account setup can point
    several app instances at one shared news DB by placing it in a shared folder.
    """
    return get_settings().db_path.parent / "news.db"


def get_news_connection() -> sqlite3.Connection:
    """Open a connection to the news DB (Row factory, WAL, thread-guard off — same
    rationale as ``shared.db.get_connection``: one connection per request, never shared
    concurrently, only create-vs-close threads may differ)."""
    path = news_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    create_tables(conn)
    return conn


@contextmanager
def news_session() -> Iterator[sqlite3.Connection]:
    """Yield a news-DB connection that commits on success, rolls back on error, closes."""
    conn = get_news_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_DDL = """
CREATE TABLE IF NOT EXISTS organized_news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    link TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    news_date TEXT NOT NULL,
    body_summary TEXT NOT NULL,
    related_stocks TEXT NOT NULL,
    source TEXT,
    lang TEXT,
    cost_usd TEXT NOT NULL DEFAULT '0',
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL,
    organized_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news_mentions (
    news_id INTEGER NOT NULL REFERENCES organized_news(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    PRIMARY KEY (news_id, symbol)
);
CREATE INDEX IF NOT EXISTS ix_news_mentions_symbol ON news_mentions(symbol);
CREATE INDEX IF NOT EXISTS ix_organized_news_date ON organized_news(news_date);
"""

# Columns added after the first schema (batch ④ cost tracking + AI attribution); added to
# a pre-existing organized_news via ALTER-if-missing so a populated news.db upgrades in place.
_ADDED_COLUMNS = (
    ("cost_usd", "TEXT NOT NULL DEFAULT '0'"),
    ("tokens_in", "INTEGER NOT NULL DEFAULT 0"),
    ("tokens_out", "INTEGER NOT NULL DEFAULT 0"),
    ("model", "TEXT"),
)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if col not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def create_tables(conn: sqlite3.Connection) -> None:
    """Create the news tables idempotently + migrate the cost columns onto older DBs."""
    conn.executescript(_DDL)
    for col, decl in _ADDED_COLUMNS:
        _add_column_if_missing(conn, "organized_news", col, decl)
    conn.commit()


class OrganizedNews(BaseModel):
    """One AI-organized news item (a stored row of ``organized_news``)."""

    link: str
    title: str
    news_date: str  # YYYY-MM-DD
    body_summary: str
    related_stocks: list[str] = Field(default_factory=list)
    source: str | None = None
    lang: str | None = None  # "zh" | "en"
    cost_usd: Decimal = Decimal("0")  # LLM cost to organize this item (0 for degrades)
    tokens_in: int = 0
    tokens_out: int = 0
    model: str | None = None  # model alias that organized this item (AI attribution)
    fetched_at: str
    organized_at: str


def link_exists(conn: sqlite3.Connection, link: str) -> bool:
    """True if this article link is already organized (skip re-fetch / re-LLM)."""
    return conn.execute(
        "SELECT 1 FROM organized_news WHERE link = ?", (link,)
    ).fetchone() is not None


def is_fully_organized(conn: sqlite3.Connection, link: str) -> bool:
    """True if this link is stored WITH an AI summary (not a headline-only degrade).

    SR fix (2026-07-06): dedup skips only fully-organized rows, so a headline-only row
    left by a one-off fetch/LLM miss is retried (and upgraded) on a later run.
    """
    row = conn.execute(
        "SELECT body_summary FROM organized_news WHERE link = ?", (link,)
    ).fetchone()
    return row is not None and bool((row["body_summary"] or "").strip())


def upsert_news(
    conn: sqlite3.Connection,
    item: OrganizedNews,
    *,
    discovered_for: str | None = None,
    index_symbols: set[str] | None = None,
) -> int:
    """Insert (or update on link) one organized news item + its mentions index.

    ``discovered_for`` is the symbol whose feed surfaced the article; it is ALWAYS
    indexed. The LLM-extracted ``related_stocks`` are stored for display, but only those
    in ``index_symbols`` (the held universe) enter the mentions index — SR fix 2026-07-06:
    an untrusted/hallucinated/injected ticker must not surface the article under an
    unrelated holding's card. ``index_symbols=None`` indexes all related_stocks (back-compat
    for tests/callers that don't constrain). Returns the news row id.

    Mentions MERGE on re-upsert (M2 fix, 2026-07-07): a link first stored headline-only
    under symbol A and later upgraded under symbol B keeps A's legitimate discovered_for
    mention — the PK (news_id, symbol) + ``INSERT OR IGNORE`` gives the natural union
    (the old DELETE-then-rewrite wiped A's mention). The ``index_symbols`` allowlist
    still constrains only the NEW mentions being added in this call.
    """
    cur = conn.execute(
        "INSERT INTO organized_news "
        "(link, title, news_date, body_summary, related_stocks, source, lang, "
        " cost_usd, tokens_in, tokens_out, model, fetched_at, organized_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(link) DO UPDATE SET title=excluded.title, news_date=excluded.news_date, "
        "body_summary=excluded.body_summary, related_stocks=excluded.related_stocks, "
        "source=excluded.source, lang=excluded.lang, cost_usd=excluded.cost_usd, "
        "tokens_in=excluded.tokens_in, tokens_out=excluded.tokens_out, "
        "model=excluded.model, organized_at=excluded.organized_at",
        (
            item.link, item.title, item.news_date, item.body_summary,
            json.dumps(item.related_stocks, ensure_ascii=False), item.source, item.lang,
            str(item.cost_usd), item.tokens_in, item.tokens_out, item.model,
            item.fetched_at, item.organized_at,
        ),
    )
    row = conn.execute(
        "SELECT id FROM organized_news WHERE link = ?", (item.link,)
    ).fetchone()
    news_id = int(row["id"]) if row is not None else int(cur.lastrowid or 0)
    related = {s for s in item.related_stocks if s}
    mentions = related if index_symbols is None else (related & index_symbols)
    if discovered_for:
        mentions.add(discovered_for)  # the discovering symbol is always trusted
    conn.executemany(
        "INSERT OR IGNORE INTO news_mentions (news_id, symbol) VALUES (?, ?)",
        [(news_id, s) for s in sorted(mentions)],
    )
    conn.commit()
    return news_id


def add_mention(conn: sqlite3.Connection, link: str, symbol: str) -> bool:
    """Add *symbol* to an already-stored article's mentions (SR fix, 2026-07-06).

    When a link is skipped by the cross-symbol dedup, the symbol whose feed surfaced it
    this run must STILL be recorded as a mention — otherwise a same-sector holding whose
    feed found an article first-ingested under another symbol would never see it. Idempotent
    (``INSERT OR IGNORE``); no-op when the link is not stored. Returns True if a row exists.
    """
    row = conn.execute("SELECT id FROM organized_news WHERE link = ?", (link,)).fetchone()
    if row is None:
        return False
    conn.execute(
        "INSERT OR IGNORE INTO news_mentions (news_id, symbol) VALUES (?, ?)",
        (int(row["id"]), symbol),
    )
    conn.commit()
    return True


def query_by_symbol(
    conn: sqlite3.Connection, symbol: str, *, since_date: str, limit: int = 10
) -> list[OrganizedNews]:
    """Organized news mentioning *symbol* with ``news_date >= since_date``, newest first.

    ``since_date`` is the inclusive lower bound (YYYY-MM-DD); the caller picks the window
    (e.g. the last 7 days). Uses the mentions index for a precise ticker match (no
    substring collisions).
    """
    rows = conn.execute(
        "SELECT o.* FROM organized_news AS o "
        "JOIN news_mentions AS m ON m.news_id = o.id "
        "WHERE m.symbol = ? AND o.news_date >= ? "
        "ORDER BY o.news_date DESC, o.id DESC LIMIT ?",
        (symbol, since_date, limit),
    ).fetchall()
    return [_from_row(r) for r in rows]


def _from_row(r: sqlite3.Row) -> OrganizedNews:
    keys = r.keys()
    return OrganizedNews(
        link=r["link"], title=r["title"], news_date=r["news_date"],
        body_summary=r["body_summary"],
        related_stocks=json.loads(r["related_stocks"]) if r["related_stocks"] else [],
        source=r["source"], lang=r["lang"],
        cost_usd=Decimal(r["cost_usd"]) if "cost_usd" in keys and r["cost_usd"] else Decimal("0"),
        tokens_in=r["tokens_in"] if "tokens_in" in keys and r["tokens_in"] else 0,
        tokens_out=r["tokens_out"] if "tokens_out" in keys and r["tokens_out"] else 0,
        model=r["model"] if "model" in keys else None,
        fetched_at=r["fetched_at"], organized_at=r["organized_at"],
    )


def distinct_symbols(conn: sqlite3.Connection) -> list[str]:
    """All symbols that appear in the mentions index (for the browse-page stock filter)."""
    return [
        r["symbol"]
        for r in conn.execute("SELECT DISTINCT symbol FROM news_mentions ORDER BY symbol")
    ]


def distinct_sources(conn: sqlite3.Connection) -> list[str]:
    """All news sources present (for the browse-page source filter)."""
    return [
        r["source"]
        for r in conn.execute(
            "SELECT DISTINCT source FROM organized_news WHERE source IS NOT NULL "
            "ORDER BY source"
        )
    ]


def query_news(
    conn: sqlite3.Connection,
    *,
    symbol: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    source: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[OrganizedNews], dict[str, Any]]:
    """Filtered news list (newest first) + aggregate totals over the WHOLE filtered set.

    Filters: ``symbol`` (precise mention match), ``date_from``/``date_to`` (inclusive
    YYYY-MM-DD), ``source``. Returns ``(rows, totals)`` where ``totals`` = ``{count,
    total_cost_usd}`` across every matching row (not just the page), for the browse
    page's cost-assessment header.
    """
    joins = ""
    clauses: list[str] = []
    params: list[Any] = []
    if symbol:
        joins = "JOIN news_mentions AS m ON m.news_id = o.id"
        clauses.append("m.symbol = ?")
        params.append(symbol)
    if date_from:
        clauses.append("o.news_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("o.news_date <= ?")
        params.append(date_to)
    if source:
        clauses.append("o.source = ?")
        params.append(source)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    # Cost summed with Decimal (never float) over EVERY matching row for the header total.
    cost_rows = conn.execute(
        f"SELECT o.cost_usd FROM organized_news AS o {joins}{where}", tuple(params)
    ).fetchall()
    total_cost = sum((Decimal(r["cost_usd"] or "0") for r in cost_rows), Decimal("0"))
    rows = conn.execute(
        f"SELECT o.* FROM organized_news AS o {joins}{where} "
        f"ORDER BY o.news_date DESC, o.id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    totals = {"count": len(cost_rows), "total_cost_usd": total_cost}
    return [_from_row(r) for r in rows], totals
