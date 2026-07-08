"""GET/PUT /api/ui-prefs — backend-persisted UI preferences (WPC, 2026-07-07).

Thin router over ``shared/ui_prefs`` (single-row config_store table). Currently one
knob: ``page_size`` — the global 每頁筆數 every pager consumer clamps against its
endpoint's own max. Counts only; no money.
"""

import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.shared.ui_prefs import (
    ALLOWED_PAGE_SIZES,
    get_ui_prefs,
    set_page_size,
)

router = APIRouter()


class UiPrefsBody(BaseModel):
    page_size: int


@router.get("/ui-prefs")
def read_ui_prefs(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, int]:
    return get_ui_prefs(conn)


@router.put("/ui-prefs")
def write_ui_prefs(
    body: UiPrefsBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    if body.page_size not in ALLOWED_PAGE_SIZES:
        allowed = " / ".join(str(v) for v in ALLOWED_PAGE_SIZES)
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"每頁筆數僅接受 {allowed}", field="page_size"))
    return set_page_size(conn, body.page_size, now=now)


__all__ = ["router"]
