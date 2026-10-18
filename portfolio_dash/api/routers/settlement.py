"""/api/settlement/* — the settlement status, run reports, change trail and period closes.

PERF spec 2026-10-18 §15. Mounted in wave 0 with no routes, so every route walker (action-log
labels, frontend callers, DEF-065) sees the routes the later packages add here.
"""

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
