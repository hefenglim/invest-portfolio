"""Common error envelope (spec 08 §8.0) + exception handlers, incl. LLM 402/409/503."""

import logging
import re
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from portfolio_dash.portfolio.cost_basis import (
    _UNCOMPUTABLE_ZH,
    OversellError,
    UnbookableLedgerError,
)
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

# --- zh-TW for everything the owner reads (QA-22 / QA-29) --------------------------------
#
# Every ``message`` in this envelope is rendered VERBATIM as a red toast
# (``window.toast(err.message, 'fail', err.code)``), so a text that was not written for the
# owner must never reach it. Three sources used to leak English:
#   * Pydantic's own ``msg`` — measured live as ``Field required`` and, from typing
#     ``1,200`` into the cash form, ``Input should be a valid decimal``;
#   * ``str(exc) or "中文"`` on the LLM exceptions — the Chinese was UNREACHABLE, because
#     every raise site supplies a non-empty English string and a non-empty string is truthy;
#   * ``str(exc.detail)`` on a Starlette ``HTTPException`` with no detail of its own, which
#     is the HTTP reason phrase: ``Not Found``, ``Method Not Allowed``.
# The static zh-TW scan cannot see any of them — it matches ``error_body(code, <literal>)``
# and all three are runtime values.

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def _prefer_zh(text: str, default: str) -> str:
    """Keep a message that was WRITTEN for the owner; replace one that was not.

    The rule is "no English reaches the owner", NOT "no detail reaches the owner". Raise
    sites that already speak Chinese (e.g. ``LLMBudgetExceeded("AI 額度用盡")``) keep their
    own words; a structural English string is swapped for the Chinese default. Replacing
    every text unconditionally would silently discard a sentence somebody wrote on purpose.
    """
    return text if text and _CJK.search(text) else default


#: HTTP status -> what it means to the person who hit it. Starlette supplies only the
#: English reason phrase for its own exceptions, and a router that raises with its own
#: Chinese detail keeps it (see ``_prefer_zh``).
_HTTP_ZH: dict[int, str] = {
    400: "請求內容有誤，請檢查後再試",
    401: "尚未登入或憑證已失效，請重新登入",
    403: "沒有權限執行這項操作",
    404: "找不到指定的資料或頁面",
    405: "這個網址不支援此操作方式",
    409: "與現有資料衝突，請重新整理後再試",
    413: "上傳內容過大，請縮小檔案或分批匯入",
    415: "不支援這種檔案格式",
    422: "資料格式正確，但內容無法處理",
    429: "操作過於頻繁，請稍後再試",
    500: "系統發生未預期的錯誤，請稍後再試",
    502: "後端服務暫時無法回應，請稍後再試",
    503: "服務暫時無法使用，請稍後再試",
    504: "後端服務回應逾時，請稍後再試",
}

#: Pydantic v2 error ``type`` -> the reason, in the owner's language. Unmapped types fall
#: back to :data:`_FALLBACK_REASON`, which is also Chinese — so a Pydantic version bump that
#: introduces a new type degrades to a vaguer Chinese sentence, never to English.
_PYDANTIC_ZH: dict[str, str] = {
    "decimal_parsing": "不是有效的數字（請移除千分位逗號、貨幣符號與空白）",
    "int_parsing": "不是有效的整數",
    "float_parsing": "不是有效的數字",
    "decimal_max_digits": "數字位數過多",
    "decimal_max_places": "小數位數過多",
    "decimal_whole_digits": "整數位數過多",
    "bool_parsing": "不是有效的是／否值",
    "date_parsing": "不是有效的日期（請用 YYYY-MM-DD）",
    "date_from_datetime_parsing": "不是有效的日期（請用 YYYY-MM-DD）",
    "datetime_parsing": "不是有效的日期時間",
    "enum": "不在可接受的選項範圍內",
    "literal_error": "不在可接受的選項範圍內",
    "string_type": "必須是文字",
    "int_type": "必須是整數",
    "float_type": "必須是數字",
    "decimal_type": "必須是數字",
    "list_type": "必須是清單",
    "dict_type": "必須是物件",
    "greater_than": "必須大於下限",
    "greater_than_equal": "小於允許的最小值",
    "less_than": "必須小於上限",
    "less_than_equal": "超過允許的最大值",
    "string_too_short": "長度不足",
    "string_too_long": "長度超過上限",
    "extra_forbidden": "不是這個表單接受的欄位",
    "json_invalid": "不是有效的 JSON 內容",
    "value_error": "內容不符合規則",
    "assertion_error": "內容不符合規則",
}
_FALLBACK_REASON = "格式不正確"

#: Defaults for the three LLM exceptions, used only when the raise site did not write its
#: own Chinese. Named constants because the handler line does not fit otherwise.
_BUDGET_ZH = "AI 額度已用盡，請調整預算或稍後再試"
_INACTIVE_ZH = "AI 功能尚未啟用，請先於設定中啟用模型"
_UNAVAILABLE_ZH = "AI 服務暫時無法使用，請稍後再試"

#: Fallback for an un-bookable ledger. Every raise site in ``portfolio/cost_basis.py`` and
#: ``portfolio/dashboard.py`` already writes its own Chinese sentence naming the row, so
#: ``_prefer_zh`` forwards those verbatim and this is the floor if one ever stops.
_UNBOOKABLE_ZH = "帳本中有一列無法入帳，請於帳本修正該列後重試"


def _echoable(value: Any) -> str | None:
    """The value the owner typed, when it is safe and useful to show it back.

    A dict or list here is the whole request body rather than a cell somebody filled in, so
    echoing it would paste the payload into a toast. Only short scalars are echoed — the
    point is that ``1,200`` appears in the message so the owner can see what to correct.
    """
    if isinstance(value, bool | int | float):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    return text if text and len(text) <= 60 else None


def _validation_message(err: Mapping[str, Any], field: str | None) -> str:
    """One Pydantic error -> one Chinese sentence naming the field and, where useful, the value."""
    where = f"欄位 {field}" if field else None
    if str(err.get("type", "")) == "missing":
        return f"{where} 為必填，請補上" if where else "請求內容不完整，請確認必填欄位都已填寫"
    reason = _PYDANTIC_ZH.get(str(err.get("type", "")), _FALLBACK_REASON)
    shown = _echoable(err.get("input"))
    if where is None:
        return f"請求內容{reason}"
    return f"{where} 的內容「{shown}」{reason}" if shown is not None else f"{where}{reason}"


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http(_r: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODE.get(exc.status_code, "error")
        default = _HTTP_ZH.get(exc.status_code, "操作失敗，請稍後再試")
        return JSONResponse(status_code=exc.status_code,
                            content=error_body(code, _prefer_zh(str(exc.detail), default)))

    @app.exception_handler(RequestValidationError)
    async def _validation(_r: Request, exc: RequestValidationError) -> JSONResponse:
        errors: list[Any] = list(exc.errors())
        first: Mapping[str, Any] = errors[0] if errors else {}
        field = ".".join(str(p) for p in first.get("loc", []) if p != "body") or None
        return JSONResponse(status_code=400,
                            content=error_body("validation_error",
                                               _validation_message(first, field),
                                               field=field))

    @app.exception_handler(LLMBudgetExceeded)
    async def _budget(_r: Request, exc: LLMBudgetExceeded) -> JSONResponse:
        return JSONResponse(status_code=402,
                            content=error_body("budget_exceeded",
                                               _prefer_zh(str(exc), _BUDGET_ZH)))

    @app.exception_handler(AINotActivated)
    async def _inactive(_r: Request, exc: AINotActivated) -> JSONResponse:
        return JSONResponse(status_code=409,
                            content=error_body("ai_not_activated",
                                               _prefer_zh(str(exc), _INACTIVE_ZH)))

    @app.exception_handler(LLMUnavailable)
    async def _unavailable(_r: Request, exc: LLMUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503,
                            content=error_body("llm_unavailable",
                                               _prefer_zh(str(exc), _UNAVAILABLE_ZH)))

    # --- the never-500 floor for the two ledger-replay faults --------------------------
    #
    # Both are USER-FIXABLE DATA problems, not server faults, and both were being answered as
    # 500 「系統發生未預期的錯誤」 by the catch-all below wherever a router had not caught them
    # by hand. ``GET /api/dashboard`` was the measured case: ``portfolio/dashboard.py`` re-types
    # a decimal fault from ``build_book`` into an ``UnbookableLedgerError`` carrying a sentence
    # the owner can act on, ``api/routers/dashboard.py`` calls ``build_dashboard`` bare, and the
    # sentence was discarded. Nine other ``build_book(..., allow_oversell=True)`` call sites
    # carry the same exposure.
    #
    # Registered as the FLOOR, not as the answer: a router that catches these locally keeps
    # precedence and can say something more specific (「無法試算」/「無法產生稅務套件」), which
    # ``strategy/whatif.py`` and ``api/routers/export.py`` both do. What changes is that
    # forgetting to catch now costs a vaguer 422 instead of an anonymous 500 — the same reason
    # ``UnbookableLedgerError`` was made a ``ValueError`` in the first place.
    #
    # TWO handlers, not one, because ``OversellError`` is a SEPARATE hierarchy (``Exception``,
    # not ``ValueError`` — ``cost_basis.py`` says so on purpose), which is exactly why it had to
    # be fixed once per router before this. Starlette dispatches on the exception's MRO, so a
    # plain ``ValueError`` is untouched by the ``UnbookableLedgerError`` arm.

    @app.exception_handler(UnbookableLedgerError)
    async def _unbookable(_r: Request, exc: UnbookableLedgerError) -> JSONResponse:
        return JSONResponse(status_code=422,
                            content=error_body("unbookable_ledger",
                                               _prefer_zh(str(exc), _UNBOOKABLE_ZH)))

    @app.exception_handler(OversellError)
    async def _oversold(_r: Request, exc: OversellError) -> JSONResponse:
        # ``str(exc)`` is English by design ("sell 100 > held 50 for 2330"), so it goes in
        # ``issues[].text`` and NEVER in ``message`` — the frontend renders ``message``
        # verbatim as a red toast. The row travels as FIELDS, the same shape
        # ``export.py`` / ``whatif.py`` emit, so no caller has to regex a sentence for it.
        return JSONResponse(status_code=422, content=error_body(
            "oversold_position",
            f"帳本中有賣超部位待釐清（{exc.account_id}／{exc.symbol}，"
            f"{exc.trade_date.isoformat()}）— 請先修正該筆交易",
            issues=[{
                "sev": "error",
                "code": "oversold_position",
                "text": str(exc),
                "field": None,
                "account_id": exc.account_id,
                "symbol": exc.symbol,
                "trade_date": exc.trade_date.isoformat(),
            }]))

    # --- the same floor for the CLASS the two owners re-type, at its LAST shared seam ----
    #
    # ``build_book`` and ``portfolio/timeseries.py`` re-type an ``ArithmeticError`` into the
    # handler above, which is why a ledger row with ``quantity='1E+999999'`` answers 422 with
    # the sentence that NAMES the row. But a ledger quantity is multiplied at three further
    # sites that neither owner can see, and the same one row was measured still answering
    # **500** at ten of sixteen (account x side x route) combinations:
    #   * ``forex/pools.py::foreign_cash_balance`` (via ``compute_fx_summary`` inside
    #     ``build_dashboard``) -> ``GET /api/dashboard`` + ``GET /api/performance/twr``, for an
    #     account whose settlement currency differs from its funding currency, SELL shape;
    #   * ``portfolio/cash.py::cash_balances`` -> ``GET /api/cash``, both sides;
    #   * ``api/routers/ledgers.py`` (``gross = t.quantity * t.price``, in the ROUTER itself,
    #     so there is no lower layer left to fix) -> ``GET /api/ledgers/transactions``.
    # Per-consumer guards cannot converge: the escape moves with the account's CURRENCY and
    # with the SIDE of the trade, and every future consumer of a ledger quantity would need
    # its own ``try``. What every one of them shares is this boundary.
    #
    # Three decisions, so the next reader does not re-litigate them:
    #  * **The traceback is logged, exactly as the catch-all does.** A ``ZeroDivisionError``
    #    from a genuine programming bug is also an ``ArithmeticError`` and also lands here, and
    #    Starlette's ``ExceptionMiddleware`` — unlike ``ServerErrorMiddleware``, which re-raises
    #    after the 500 handler — does NOT re-raise what a handler answered. Without this line
    #    the diagnosis would be gone. The 422 makes the answer *safe*; the log keeps it
    #    *diagnosable*.
    #  * **``str(exc)`` never reaches ``message``.** ``message`` is rendered verbatim as a red
    #    toast and a trapped ``decimal.Overflow`` stringifies to its signal list; the text is
    #    the constant, no ``_prefer_zh`` echo. Same reason the catch-all leaks nothing.
    #  * **One owner for the sentence.** ``_UNCOMPUTABLE_ZH`` is imported from
    #    ``portfolio/cost_basis.py`` rather than copied, so the generic wording cannot fork
    #    from the one the replay itself emits when it cannot name a row.
    # ``UnbookableLedgerError`` keeps precedence by construction: it is a ``ValueError``, so
    # ``ArithmeticError`` is not in its MRO and Starlette's most-specific-class dispatch never
    # reaches this arm — the precise, row-naming sentence is not degraded by the net.

    @app.exception_handler(ArithmeticError)
    async def _arithmetic(_r: Request, exc: ArithmeticError) -> JSONResponse:
        logger.exception("uncomputable ledger value: %s", exc)
        return JSONResponse(status_code=422,
                            content=error_body("unbookable_ledger", _UNCOMPUTABLE_ZH))

    @app.exception_handler(Exception)
    async def _unhandled(_r: Request, exc: Exception) -> JSONResponse:
        # Catch-all: the specific handlers above keep precedence for their own types;
        # this records the traceback (via the JSON formatter's exc_info path) and returns
        # a generic envelope WITHOUT leaking the exception detail into the response body.
        logger.exception("unhandled error: %s", exc)
        return JSONResponse(status_code=500,
                            content=error_body("internal_error",
                                               "系統發生未預期的錯誤，請稍後再試"))
