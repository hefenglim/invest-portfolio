"""Common error envelope (spec 08 §8.0) + exception handlers, incl. LLM 402/409/503."""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from portfolio_dash.shared.llm_config import (
    AINotActivated,
    LLMBudgetExceeded,
    LLMUnavailable,
)

logger = logging.getLogger(__name__)


def error_body(code: str, message: str, *, field: str | None = None,
               issues: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if field is not None:
        err["field"] = field
    if issues is not None:
        err["issues"] = issues
    return {"error": err}


_STATUS_CODE = {400: "validation_error", 401: "unauthorized", 403: "forbidden",
                404: "not_found", 422: "unprocessable", 500: "internal_error"}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http(_r: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODE.get(exc.status_code, "error")
        return JSONResponse(status_code=exc.status_code,
                            content=error_body(code, str(exc.detail)))

    @app.exception_handler(RequestValidationError)
    async def _validation(_r: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(p) for p in first.get("loc", []) if p != "body") or None
        return JSONResponse(status_code=400,
                            content=error_body("validation_error",
                                               first.get("msg", "invalid request"),
                                               field=field))

    @app.exception_handler(LLMBudgetExceeded)
    async def _budget(_r: Request, exc: LLMBudgetExceeded) -> JSONResponse:
        return JSONResponse(status_code=402,
                            content=error_body("budget_exceeded", str(exc) or "AI 額度用盡"))

    @app.exception_handler(AINotActivated)
    async def _inactive(_r: Request, exc: AINotActivated) -> JSONResponse:
        return JSONResponse(status_code=409,
                            content=error_body("ai_not_activated", str(exc) or "AI 未啟用"))

    @app.exception_handler(LLMUnavailable)
    async def _unavailable(_r: Request, exc: LLMUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503,
                            content=error_body("llm_unavailable", str(exc) or "LLM 服務不可用"))

    @app.exception_handler(Exception)
    async def _unhandled(_r: Request, exc: Exception) -> JSONResponse:
        # Catch-all: the specific handlers above keep precedence for their own types;
        # this records the traceback (via the JSON formatter's exc_info path) and returns
        # a generic envelope WITHOUT leaking the exception detail into the response body.
        logger.exception("unhandled error: %s", exc)
        return JSONResponse(status_code=500,
                            content=error_body("internal_error", "internal error"))
