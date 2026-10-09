"""DEF-008 (functional test manual A-05 / A-06, 2026-09-23): a toast shows words, not codes.

``web/api.js`` documents the convention every failure path follows —
``window.toast(err.message, 'fail', err.code)`` — and ``window.toast`` rendered its third
argument as a visible sub-line. So every refused request printed the machine code under the
zh sentence: 「… 出金不可透支 withdraw_insufficient_balance」, 「… validation_error」.

The fix is at the ONE seam every caller shares (``web/shell.js``'s ``window.toast``), not at
the ~50 call sites: a sub that is a machine code — lower_snake ASCII — moves to the toast's
tooltip and the console. This file pins the two halves of that contract:

* the filter exists, runs BEFORE the de-duplication key and the sub-line are built, and
  writes the tooltip;
* every code the backend can put in an error envelope (every string literal passed as the
  first argument of ``error_body``) matches the filter — so a new code cannot slip past it —
  while no zh sub-line a caller passes can match it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SHELL = (_ROOT / "web" / "shell.js").read_text(encoding="utf-8")


def _toast_body() -> str:
    m = re.search(r"window\.toast = function \(msg, kind, sub\) \{(.*?)\n  \};", _SHELL, re.S)
    assert m, "window.toast not found"
    return m.group(1)


def _diag_pattern() -> re.Pattern[str]:
    m = re.search(r"const DIAG_CODE = /(.+?)/;", _SHELL)
    assert m, "shell.js has no DIAG_CODE filter"
    return re.compile(m.group(1))


def test_the_toast_moves_a_code_to_the_tooltip_before_it_renders_anything() -> None:
    body = _toast_body()
    filt = body.find("DIAG_CODE.test(sub)")
    assert filt >= 0, "window.toast does not test its sub-line for a machine code"
    assert filt < body.find("const key"), "the filter must run before the dedupe key"
    assert filt < body.find("'sub', sub"), "the filter must run before the sub-line renders"
    assert "t.title = '錯誤碼：' + diag" in body, "the code must stay reachable (tooltip)"


def _backend_error_codes() -> set[str]:
    """Every ``error_body("<code>", …)`` literal, plus every ``Issue(kind="<code>")`` literal
    — a door forwards an Issue's kind as the envelope code (``withdraw_insufficient_balance``
    reaches the wire as ``hard.kind``, never as a literal)."""
    codes: set[str] = set()
    for path in (_ROOT / "portfolio_dash").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if (node.func.id == "error_body" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                codes.add(node.args[0].value)
            if node.func.id == "Issue":
                for kw in node.keywords:
                    if (kw.arg == "kind" and isinstance(kw.value, ast.Constant)
                            and isinstance(kw.value.value, str)):
                        codes.add(kw.value.value)
    return codes


def test_every_backend_error_code_is_hidden_by_the_filter() -> None:
    pattern = _diag_pattern()
    codes = _backend_error_codes()
    # Detection power: the scan found the codes the verifier saw on screen.
    assert {"withdraw_insufficient_balance", "validation_error", "negative_cash"} <= codes
    leaking = sorted(c for c in codes if not pattern.fullmatch(c))
    assert not leaking, f"these codes would print under a toast: {leaking}"


def test_a_zh_sub_line_is_never_mistaken_for_a_code() -> None:
    pattern = _diag_pattern()
    for sub in ("正在依匯率試算；也可直接填入實際成交金額", "刪除 3 筆", "出金 1000",
                "v12 已封存", "TSLA：yfinance 逾時", ""):
        assert not pattern.fullmatch(sub), sub
    for code in ("forbidden", "error", "oversold_position", "fx_insufficient_balance"):
        assert pattern.fullmatch(code), code
