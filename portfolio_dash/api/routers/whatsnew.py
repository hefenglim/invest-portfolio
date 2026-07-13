"""GET/POST /api/whats-new — feature-announcement panel + per-feature seen-state, and
GET /api/whats-new/history — the full paged version-release browser.

Thin router over ``shared/whatsnew`` (static CATALOG + a per-feature seen table). It
serializes the visible per-version feature groups and the unseen badge count; POST marks
individual features (or all of the visible window) seen. History pages the FULL catalog
(version <= current, newest first) via ``offset/limit`` — the 6-version cap applies ONLY
to the ✦ panel. Paging is the scalability answer: the client only renders the pages it
loads, so an ever-growing catalog cannot degrade page performance. Counts and strings
only; no money, no business calculation — the model owns the ordering/visibility logic.
"""

import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import portfolio_dash
from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.shared.whatsnew import (
    CATALOG,
    VERSION_DATES,
    Feature,
    all_visible_versions,
    get_seen_keys,
    known_feature_keys,
    mark_seen,
    visible_versions,
)

router = APIRouter()


class SeenBody(BaseModel):
    # Mark a specific set of feature keys seen, OR (all=true) every key in the visible
    # window. ``all`` shadows no BaseModel member; ruff enables no builtin-shadowing rule.
    features: list[str] | None = None
    all: bool = False


def _feature_json(feature: Feature, key: str, seen: bool) -> dict[str, Any]:
    return {
        "key": key,
        "id": feature.id,
        "title": feature.title,
        "desc": feature.desc,
        "href": feature.href,
        "area": feature.area,
        "target": feature.target,
        "seen": seen,
    }


def _payload(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assemble the GET/POST response: current version, unseen badge count, groups."""
    current = portfolio_dash.__version__
    seen_keys = get_seen_keys(conn)
    versions: list[dict[str, Any]] = []
    unseen_count = 0
    for version in visible_versions(current):
        feat_jsons: list[dict[str, Any]] = []
        group_unseen = False
        for feature in (f for f in CATALOG if f.version == version):
            key = f"{version}:{feature.id}"
            is_seen = key in seen_keys
            if not is_seen:
                group_unseen = True
                unseen_count += 1
            feat_jsons.append(_feature_json(feature, key, is_seen))
        versions.append({
            "version": version,
            "date": VERSION_DATES.get(version),
            "unseen": group_unseen,
            "features": feat_jsons,
        })
    return {
        "current_version": current,
        "unseen_count": unseen_count,
        "versions": versions,
    }


@router.get("/whats-new")
def read_whats_new(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return _payload(conn)


@router.post("/whats-new/seen")
def write_seen(
    body: SeenBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    """Mark features seen. Body: ``{"features": ["<version>:<id>", ...]}`` OR ``{"all": true}``.

    Every supplied key must be in the visible window; an unknown key → 400, nothing
    written. ``all`` marks every key in the visible window. Returns the GET payload.
    Idempotent.
    """
    current = portfolio_dash.__version__
    known = known_feature_keys(current)
    if body.all:
        keys = sorted(known)
    else:
        keys = body.features or []
        unknown = [k for k in keys if k not in known]
        if unknown:
            return JSONResponse(status_code=400, content=error_body(
                "validation_error", "包含未知或不在可見範圍內的功能項目", field="features"))
    mark_seen(conn, keys, now=now)
    return _payload(conn)


@router.get("/whats-new/history")
def read_history(
    offset: int = 0,
    limit: int = 5,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """Page the FULL release history (version <= current, newest first).

    ``offset >= 0`` and ``1 <= limit <= 20`` (else 400). Each version group carries
    ``version``, ``date`` (nullable), and user-facing ``features[]`` (title/desc/area — no
    seen-state, no NEW pills). ``total`` is constant across pages so the client knows when
    to stop.
    """
    if offset < 0:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "offset 不可為負數", field="offset"))
    if limit < 1 or limit > 20:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "limit 必須介於 1 到 20", field="limit"))
    current = portfolio_dash.__version__
    ordered = all_visible_versions(current)
    page = ordered[offset:offset + limit]
    versions: list[dict[str, Any]] = []
    for version in page:
        versions.append({
            "version": version,
            "date": VERSION_DATES.get(version),
            "features": [
                {"title": f.title, "desc": f.desc, "area": f.area}
                for f in CATALOG if f.version == version
            ],
        })
    return {"total": len(ordered), "offset": offset, "versions": versions}


__all__ = ["router"]
