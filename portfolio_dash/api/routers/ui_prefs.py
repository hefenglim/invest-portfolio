"""GET/PUT /api/ui-prefs — backend-persisted UI preferences (WPC, 2026-07-07).

Thin router over ``shared/ui_prefs`` (single-row config_store table). Two knobs:
``page_size`` — the global 每頁筆數 every pager consumer clamps against its endpoint's own
max — and ``auto_ai_resolve`` (2026-09-16, demo audit L13) — whether the 觀察清單
quick-add may fire the paid AI resolver by itself on a name-like miss. PUT is a subset
merge: send only the field being changed. Counts and flags only; no money.
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
    set_ui_prefs,
)

router = APIRouter()


class UiPrefsBody(BaseModel):
    page_size: int | None = None
    auto_ai_resolve: bool | None = None


@router.get("/ui-prefs")
def read_ui_prefs(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    return get_ui_prefs(conn)


@router.put("/ui-prefs")
def write_ui_prefs(
    body: UiPrefsBody,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    if body.page_size is None and body.auto_ai_resolve is None:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "請至少提供一個要更新的偏好設定"))
    if body.page_size is not None and body.page_size not in ALLOWED_PAGE_SIZES:
        allowed = " / ".join(str(v) for v in ALLOWED_PAGE_SIZES)
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"每頁筆數僅接受 {allowed}", field="page_size"))
    return set_ui_prefs(
        conn, page_size=body.page_size, auto_ai_resolve=body.auto_ai_resolve, now=now
    )


__all__ = ["router"]
