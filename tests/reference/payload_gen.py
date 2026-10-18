"""Render a corpus's reference payloads (plan W0.2 (a), P9).

ONE renderer for both ends: ``scripts/perf/make_reference_payloads.py`` runs it inside a
worktree of commit 473fe5a to WRITE the files, and ``tests/reference/test_reference_payloads.py``
runs it on the current code to COMPARE them. So it must use only APIs that exist at 473fe5a.

For each corpus (``tests/reference/corpora.py``), at its fixed ``now`` and reporting currency:

* ``dashboard.json`` — the exact response body bytes of ``GET /api/dashboard``;
* ``tax_<year>/<member>`` — every member of ``POST /api/export/tax-package``'s zip (the three
  CSVs and ``summary.md``), for every year with ledger activity; a refusal is stored as
  ``tax_<year>/response.json`` (status + body) instead. The zip itself is NOT stored: its
  member timestamps are the wall clock;
* ``month_end/<YYYY-MM>.json`` — the "truncated world" dashboard of every month end D with
  ``first ledger date <= D < now.date()``: a COPY of the database with every ``prices`` and
  ``fx_rates`` row dated after D deleted, ``GET /api/dashboard`` at D 23:59 Asia/Taipei, reduced
  to :func:`period_subset`.

Every request goes through ``create_app()`` with ``get_conn`` / ``get_now`` / ``get_reporting``
overridden and no lifespan — exactly ``tests/conftest.py::api_client``. Each output is built on
its own fresh copy of the template, so no request's writes (``composer_store.ensure_seeded``
on the GET, the action-log row of the POST) can reach another output.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from portfolio_dash.api.app import create_app
from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.shared.enums import Currency
from tests.reference.corpora import (
    CORPORA,
    TAIPEI,
    copy_db,
    ensure_template,
    fingerprint,
    first_ledger_date,
    open_db,
)

#: Top-level dashboard keys a period close does not carry: router add-ons that read
#: tables other than the ledger (AI cards, the AI quota) or are rules over the whole page
#: (alerts). Everything else in the payload is kept.
PERIOD_DROP_KEYS: tuple[str, ...] = ("alerts", "insights", "llm_quota")
#: Holding-row keys dropped: the 30-day sparkline is a series, not a figure at D.
PERIOD_DROP_HOLDING_KEYS: tuple[str, ...] = ("spark_30d",)


def month_end_now(d: date) -> datetime:
    """The clock of a month-end close: D 23:59 Asia/Taipei."""
    return datetime(d.year, d.month, d.day, 23, 59, tzinfo=TAIPEI)


def month_ends(first: date, now_day: date) -> list[date]:
    """Every calendar month end D with ``first <= D < now_day``."""
    out: list[date] = []
    y, m = first.year, first.month
    while True:
        nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        end = nxt - timedelta(days=1)
        if end >= now_day:
            return out
        if end >= first:
            out.append(end)
        y, m = nxt.year, nxt.month


def tax_years(first: date, now_day: date) -> list[int]:
    """Every calendar year from the first ledger date's to the clock's."""
    return list(range(first.year, now_day.year + 1))


def new_app() -> FastAPI:
    """The app every request of one :func:`render_corpus` call goes through. Built once per
    corpus (building one costs ~1 s for 214 routes, a dashboard request ~0.15 s) and never
    shared across corpora; each request sets, and then clears, its own overrides."""
    return create_app()


@contextmanager
def _client(app: FastAPI, db_path: Path, now: datetime, reporting: Currency
            ) -> Iterator[TestClient]:
    conn = open_db(db_path)
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_now] = lambda: now
    app.dependency_overrides[get_reporting] = lambda: reporting
    try:
        yield TestClient(app)  # no `with`: the lifespan is not run (hermetic, as api_client)
    finally:
        app.dependency_overrides.clear()
        conn.close()


def dashboard_body(app: FastAPI, db_path: Path, now: datetime, reporting: Currency) -> bytes:
    """The exact response body of ``GET /api/dashboard``."""
    with _client(app, db_path, now, reporting) as client:
        resp = client.get("/api/dashboard")
    if resp.status_code != 200:
        raise RuntimeError(f"/api/dashboard answered {resp.status_code}: {resp.text[:500]}")
    body: bytes = resp.content
    return body


def tax_package(app: FastAPI, db_path: Path, now: datetime, reporting: Currency, year: int
                ) -> dict[str, bytes]:
    """The tax package's members (name -> bytes), or ``{"response.json": …}`` on a refusal."""
    with _client(app, db_path, now, reporting) as client:
        resp = client.post("/api/export/tax-package", json={"year": year})
    if resp.status_code != 200:
        return {"response.json": canonical_json(
            {"status": resp.status_code, "body": resp.json()})}
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        return {info.filename: zf.read(info) for info in zf.infolist()}


def truncate_world(db_path: Path, d: date) -> None:
    """Delete every price and FX row dated after *d* (the PERF-S7 "as at D" world)."""
    conn = open_db(db_path)
    try:
        conn.execute("DELETE FROM prices WHERE as_of_date > ?", (d.isoformat(),))
        conn.execute("DELETE FROM fx_rates WHERE as_of_date > ?", (d.isoformat(),))
        conn.commit()
    finally:
        conn.close()


def period_subset(payload: dict[str, Any], d: date) -> dict[str, Any]:
    """What a period close needs from a truncated-world dashboard, in payload order.

    Every key except :data:`PERIOD_DROP_KEYS`; holding rows without
    :data:`PERIOD_DROP_HOLDING_KEYS`; ``trend.points`` reduced to the single point dated D
    (empty when the trend has none — e.g. unavailable). The point carries ``total_value``,
    ``net_invested``, ``incomplete`` and ``net_worth`` — the settled day D. Nothing is
    re-formatted: every value is the server's own string.
    """
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key in PERIOD_DROP_KEYS:
            continue
        if key == "holdings":
            value = [{k: v for k, v in row.items() if k not in PERIOD_DROP_HOLDING_KEYS}
                     for row in value]
        elif key == "trend" and isinstance(value, dict):
            value = dict(value)
            value["points"] = [p for p in value.get("points", [])
                               if p.get("date") == d.isoformat()]
        out[key] = value
    return out


def canonical_json(obj: Any) -> bytes:
    """The committed form of a derived JSON file: UTF-8, one space indent, LF, trailing LF."""
    return (json.dumps(obj, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def month_end_body(app: FastAPI, template: Path, work: Path, d: date, reporting: Currency
                   ) -> bytes:
    """The period subset of the truncated-world dashboard at month end *d*."""
    db = copy_db(template, work / f"month_end_{d.isoformat()}.db")
    truncate_world(db, d)
    raw = dashboard_body(app, db, month_end_now(d), reporting)
    return canonical_json(period_subset(json.loads(raw), d))


def corpus_info(name: str, template_dir: Path) -> dict[str, Any]:
    """The facts a corpus's reference files are generated from (recorded in the manifest)."""
    corpus = CORPORA[name]
    conn = open_db(ensure_template(name, template_dir))
    try:
        first = first_ledger_date(conn)
        digest = fingerprint(conn)
    finally:
        conn.close()
    now_day = corpus.now.date()
    return {
        "now": corpus.now.isoformat(),
        "reporting": corpus.reporting.value,
        "summary": corpus.summary,
        "fingerprint": digest,
        "first_ledger_date": None if first is None else first.isoformat(),
        "tax_years": [] if first is None else tax_years(first, now_day),
        "month_ends": [] if first is None else [d.isoformat()
                                                for d in month_ends(first, now_day)],
    }


def render_corpus(
    name: str,
    template_dir: Path,
    work: Path,
    *,
    month_filter: Callable[[list[date]], Iterable[date]] | None = None,
) -> dict[str, bytes]:
    """Every reference file of corpus *name*: ``{relative path: bytes}``.

    *month_filter* picks which month ends to build (default: all of them).
    """
    corpus = CORPORA[name]
    template = ensure_template(name, template_dir)
    info = corpus_info(name, template_dir)
    work.mkdir(parents=True, exist_ok=True)
    app = new_app()
    files: dict[str, bytes] = {}

    files["dashboard.json"] = dashboard_body(
        app, copy_db(template, work / "dashboard.db"), corpus.now, corpus.reporting)

    for year in info["tax_years"]:
        db = copy_db(template, work / f"tax_{year}.db")
        for member, data in tax_package(app, db, corpus.now, corpus.reporting, year).items():
            files[f"tax_{year}/{member}"] = data

    ends = [date.fromisoformat(d) for d in info["month_ends"]]
    chosen = ends if month_filter is None else list(month_filter(ends))
    for d in chosen:
        files[f"month_end/{d:%Y-%m}.json"] = month_end_body(
            app, template, work, d, corpus.reporting)
    return files


__all__ = [
    "PERIOD_DROP_HOLDING_KEYS",
    "PERIOD_DROP_KEYS",
    "canonical_json",
    "corpus_info",
    "dashboard_body",
    "month_end_body",
    "month_end_now",
    "month_ends",
    "new_app",
    "period_subset",
    "render_corpus",
    "tax_package",
    "tax_years",
    "truncate_world",
]
