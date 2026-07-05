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
from pathlib import Path

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


def create_tables(conn: sqlite3.Connection) -> None:
    """Create the news tables idempotently."""
    conn.executescript(_DDL)
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
    fetched_at: str
    organized_at: str


def link_exists(conn: sqlite3.Connection, link: str) -> bool:
    """True if this article link is already organized (skip re-fetch / re-LLM)."""
    return conn.execute(
        "SELECT 1 FROM organized_news WHERE link = ?", (link,)
    ).fetchone() is not None


def upsert_news(
    conn: sqlite3.Connection, item: OrganizedNews, *, discovered_for: str | None = None
) -> int:
    """Insert (or update on link) one organized news item + its mentions index.

    ``discovered_for`` is the symbol whose feed surfaced the article; it is always added
    to the mentions set (union with the LLM-extracted ``related_stocks``) so a card for
    that symbol finds the news even if the model did not name the ticker in the body.
    Returns the news row id.
    """
    cur = conn.execute(
        "INSERT INTO organized_news "
        "(link, title, news_date, body_summary, related_stocks, source, lang, "
        " fetched_at, organized_at) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(link) DO UPDATE SET title=excluded.title, news_date=excluded.news_date, "
        "body_summary=excluded.body_summary, related_stocks=excluded.related_stocks, "
        "source=excluded.source, lang=excluded.lang, organized_at=excluded.organized_at",
        (
            item.link, item.title, item.news_date, item.body_summary,
            json.dumps(item.related_stocks, ensure_ascii=False), item.source, item.lang,
            item.fetched_at, item.organized_at,
        ),
    )
    row = conn.execute(
        "SELECT id FROM organized_news WHERE link = ?", (item.link,)
    ).fetchone()
    news_id = int(row["id"]) if row is not None else int(cur.lastrowid or 0)
    mentions = {s for s in item.related_stocks if s}
    if discovered_for:
        mentions.add(discovered_for)
    conn.execute("DELETE FROM news_mentions WHERE news_id = ?", (news_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO news_mentions (news_id, symbol) VALUES (?, ?)",
        [(news_id, s) for s in sorted(mentions)],
    )
    conn.commit()
    return news_id


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
    return OrganizedNews(
        link=r["link"], title=r["title"], news_date=r["news_date"],
        body_summary=r["body_summary"],
        related_stocks=json.loads(r["related_stocks"]) if r["related_stocks"] else [],
        source=r["source"], lang=r["lang"],
        fetched_at=r["fetched_at"], organized_at=r["organized_at"],
    )
