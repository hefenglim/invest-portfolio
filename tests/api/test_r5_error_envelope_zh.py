"""R5 / QA-22 + QA-29: nothing in the error envelope's ``message`` may be raw English.

``api/errors.py`` had three leaks, all of them shown to the owner as a red toast (``web``'s
callers do ``window.toast(err.message, 'fail', err.code)``):

* ``first.get("msg", "invalid request")`` forwarded **Pydantic's own English** — measured
  live: ``Field required``, and ``Input should be a valid decimal`` from typing ``1,200``
  into the cash form, which is an ordinary thing for a person to type.
* ``str(exc) or "AI 額度用盡"`` never reached the Chinese, because every raise site in
  ``shared/llm.py`` / ``shared/llm_config.py`` supplies a NON-EMPTY English string and a
  non-empty string is truthy. The Chinese default was unreachable code.
* ``str(exc.detail)`` on a Starlette ``HTTPException`` that carries no detail of its own is
  the HTTP reason phrase — ``Not Found`` / ``Method Not Allowed``.

The last test in this file is the generalisation: ONE assertion over the real app that no
user-facing ``message`` is ASCII-only. The static scan
(``tests/architecture/test_user_messages_are_zh_tw.py``) cannot cover these, because it
matches ``Issue(message=<literal>)`` / ``error_body(code, <literal>)`` and every leak above
is a runtime VALUE, not a literal.
"""

import re
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api.errors import register_error_handlers
from portfolio_dash.shared.llm_config import (
    AINotActivated,
    LLMBudgetExceeded,
    LLMUnavailable,
)

_ASCII_ONLY = re.compile(r"^[\x00-\x7f]*$")
_CJK = re.compile(r"[一-鿿]")

_MOVEMENT = {"account_id": "tw_broker", "date": "2026-07-01", "kind": "deposit",
             "ccy": "TWD", "amount": "100"}


@pytest.fixture
def raising_client() -> Iterator[Callable[[Exception], TestClient]]:
    """A one-route app wired to the REAL handlers, whose route raises *exc*.

    Sockets are re-enabled exactly as ``conftest.api_client`` documents: on Windows the
    TestClient's anyio portal runs a ProactorEventLoop whose self-pipe is a real socket,
    which the global ``--disable-socket`` ban blocks. Nothing leaves the process.
    """
    enable_socket()

    def _make(exc: Exception) -> TestClient:
        app = FastAPI()
        register_error_handlers(app)

        @app.get("/boom")
        def _boom() -> None:
            raise exc

        return TestClient(app, raise_server_exceptions=False)

    try:
        yield _make
    finally:
        disable_socket(allow_unix_socket=True)


def _message(client: TestClient, method: str, path: str, body: object | None) -> str:
    resp = (client.request(method, path, json=body) if body is not None
            else client.request(method, path))
    assert 400 <= resp.status_code < 500, f"{method} {path} -> {resp.status_code}"
    return str(resp.json()["error"]["message"])


def test_a_missing_required_field_is_reported_in_chinese(api_client: TestClient) -> None:
    """QA-22, half one: the live reproduction was ``{'message': 'Field required'}``."""
    body = {k: v for k, v in _MOVEMENT.items() if k != "amount"}
    msg = _message(api_client, "POST", "/api/cash/movements", body)
    assert not _ASCII_ONLY.match(msg), msg
    assert _CJK.search(msg), msg
    assert "amount" in msg, msg          # which field is missing, or the toast is useless


def test_a_thousands_separator_is_reported_in_chinese(api_client: TestClient) -> None:
    """QA-22, half two: typing ``1,200`` into the cash form gave 'Input should be a valid
    decimal'. The owner types thousands separators; the app must answer in their language."""
    msg = _message(api_client, "POST", "/api/cash/movements",
                   {**_MOVEMENT, "amount": "1,200"})
    assert not _ASCII_ONLY.match(msg), msg
    assert _CJK.search(msg), msg
    assert "amount" in msg, msg
    assert "1,200" in msg, msg           # echo what was typed, so it can be corrected


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        # The texts are the REAL raise sites' texts (shared/llm.py, shared/llm_config.py).
        (LLMBudgetExceeded("token budget exhausted (remaining $0.00)"), 402),
        (AINotActivated("no enabled model configured for the input role"), 409),
        (LLMUnavailable("provider error (gpt-x): connection refused"), 503),
    ],
    ids=["budget", "not_activated", "unavailable"],
)
def test_llm_exception_text_does_not_reach_the_owner(
    exc: Exception, status: int,
    raising_client: Callable[[Exception], TestClient],
) -> None:
    """QA-29 (a): ``str(exc) or "中文"`` never reached the Chinese."""
    resp = raising_client(exc).get("/boom")
    assert resp.status_code == status
    msg = str(resp.json()["error"]["message"])
    assert not _ASCII_ONLY.match(msg), msg
    assert _CJK.search(msg), msg


def test_a_chinese_exception_text_is_still_forwarded_verbatim(
    raising_client: Callable[[Exception], TestClient],
) -> None:
    """The complement: a raise site that DOES speak Chinese keeps its own words.

    ``tests/contract/test_instruments_ai_resolve.py`` raises ``LLMBudgetExceeded("AI 額度
    用盡")``; replacing every text with a fixed default would silently discard a message
    someone wrote on purpose. The rule is 'no English reaches the owner', not 'no detail'.
    """
    resp = raising_client(LLMUnavailable("AI 服務忙碌中，請稍後再試")).get("/boom")
    assert resp.json()["error"]["message"] == "AI 服務忙碌中，請稍後再試"


# ---------------------------------------------------------------------------------------
# The generalisation: one sweep, every shape of 4xx the envelope can produce.
#
# Each row was executed against the UNFIXED code and returned an ASCII-only message; they
# are kept together so a new leak of this class fails on the sweep rather than waiting for
# someone to file it. Every entry is a request a person could actually make.
_CASES: list[tuple[str, str, object | None]] = [
    ("POST", "/api/cash/movements", {k: v for k, v in _MOVEMENT.items() if k != "amount"}),
    ("POST", "/api/cash/movements", {**_MOVEMENT, "amount": "1,200"}),
    ("POST", "/api/cash/movements", {**_MOVEMENT, "amount": "NT$1200"}),
    ("POST", "/api/cash/movements", {**_MOVEMENT, "date": "07/01/2026"}),
    ("POST", "/api/cash/movements", {**_MOVEMENT, "ccy": "XXX"}),
    ("POST", "/api/cash/movements", {**_MOVEMENT, "ack_negative": "maybe"}),
    ("POST", "/api/cash/movements", {**_MOVEMENT, "note": 5}),
    ("POST", "/api/cash/fx", {}),
    ("PUT", "/api/ledgers/transactions/1",
     {"account_id": "tw_broker", "symbol": "2330", "side": "SELL", "date": "2026-02-01",
      "shares": "1e13", "price": "1", "fee": "-1", "tax": "0"}),
    # No body at all: the handler must not fall through to Pydantic's English either.
    ("POST", "/api/cash/movements", None),
    # Starlette's own exceptions, which carry an English reason phrase as their detail.
    ("GET", "/api/does-not-exist", None),
    ("POST", "/api/dashboard", {}),
]


@pytest.mark.parametrize(("method", "path", "body"), _CASES,
                         ids=[f"{m}{p}{i}" for i, (m, p, _b) in enumerate(_CASES)])
def test_no_user_facing_message_is_ascii_only(
    api_client: TestClient, method: str, path: str, body: object | None,
) -> None:
    resp = (api_client.request(method, path, json=body) if body is not None
            else api_client.request(method, path))
    assert 400 <= resp.status_code < 600, f"{method} {path} -> {resp.status_code}"
    msg = str(resp.json()["error"]["message"])
    assert msg, f"{method} {path}: empty message"
    assert not _ASCII_ONLY.match(msg), (
        f"{method} {path} -> {resp.status_code}: the owner reads this verbatim as a red "
        f"toast, and it is English: {msg!r}")
