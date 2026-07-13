"""GET/POST /api/whats-new — feature-announcement panel + acknowledged-version state.

Thin router over ``shared/whatsnew`` (static CATALOG + single-row seen-state table). It
serializes the visible per-version feature groups and the unseen badge count; POST
acknowledges up to the current version (monotonic in the store). Counts and strings
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
    _version_key,
    get_seen_version,
    is_valid_version,
    set_seen_version,
    visible_versions,
)

router = APIRouter()


class SeenBody(BaseModel):
    version: str


def _feature_json(feature: Feature) -> dict[str, Any]:
    return {
        "id": feature.id,
        "title": feature.title,
        "desc": feature.desc,
        "href": feature.href,
        "area": feature.area,
    }


def _payload(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assemble the GET/POST response: current + seen version, badge count, groups."""
    current = portfolio_dash.__version__
    seen = get_seen_version(conn)
    seen_key = _version_key(seen)
    versions: list[dict[str, Any]] = []
    unseen_count = 0
    for version in visible_versions(current):
        features = [f for f in CATALOG if f.version == version]
        unseen = _version_key(version) > seen_key
        if unseen:
            unseen_count += len(features)
        versions.append({
            "version": version,
            "date": VERSION_DATES.get(version),
            "unseen": unseen,
            "features": [_feature_json(f) for f in features],
        })
    return {
        "current_version": current,
        "seen_version": seen,
        "unseen_count": unseen_count,
        "versions": versions,
    }


@router.get("/whats-new")
def read_whats_new(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return _payload(conn)


@router.post("/whats-new/seen")
def mark_seen(
    body: SeenBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    if not is_valid_version(body.version):
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "版本格式無效", field="version"))
    # Clamp to the running version: acknowledging "beyond" current would permanently
    # suppress the badge for every FUTURE release (irreversible from the UI). The store
    # stays monotonic; this cap just bounds how far a single POST can advance it.
    current = portfolio_dash.__version__
    version = body.version if _version_key(body.version) <= _version_key(current) else current
    set_seen_version(conn, version, now=now)
    return _payload(conn)


__all__ = ["router"]
