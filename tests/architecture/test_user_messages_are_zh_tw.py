"""Every message the OWNER reads is Traditional Chinese (CLAUDE.md bilingual protocol).

The protocol says repository artifacts — code, docstrings, comments, commits, CHANGELOG —
are English, and what reaches the human is zh-TW. Error messages are the second kind: they
are rendered verbatim by the frontend (``web/input.js`` puts ``issue.text`` and
``row.reason`` straight into the cell), so an English one is a user-visible defect, not a
style preference.

Found 2026-08-05 by the 0->1 sweep. A first-time user hits these constantly — every symbol
is unregistered on day one — and 21 of them were English. The clincher was inside a single
``validate_transaction`` call: ``"quantity must be > 0"`` sat four lines from
``"股數過大,無法處理"``. Not a decision; drift. Without this guard the 22nd arrives next
release, so the rule is enforced rather than remembered.

The scan resolves module-level string constants, because ``f"{symbol} {_HAS_HISTORY_MSG}"``
carries its Chinese in the constant — a literals-only scan would flag it falsely and the
allowlist would then be hiding a real check.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "portfolio_dash"
_CJK = re.compile(r"[一-鿿]")

# A snake_case identifier is a CODE, not display text — `stop_reason =
# "budget_exhausted_mid_run"` is stored in `job_runs.reason` and mapped to Chinese by the
# frontend's SKIP_REASONS table, the same way `Issue.kind` is. Codes stay English on
# purpose: they are stable across releases and greppable in logs.
#
# This is a rule, not an allowlist, because the allowlist would need a new entry for every
# code added — and a control that depends on remembering is not a control. What keeps a
# code honest is the OTHER guard: tests/contract/test_skip_reason_coverage.py fails if the
# frontend has no label for it, which is how the four `*_mid_run` codes were caught
# rendering raw on screen. Display text always has a space or punctuation; a bare
# identifier never does.
_CODE_SHAPED = re.compile(r"^[a-z][a-z0-9_]*$")

# Texts that are legitimately English AND not code-shaped. Each entry is a claim that the
# owner never reads this string — verify it before adding one.
_ALLOWED: frozenset[str] = frozenset()


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "..."`` string constants, for resolving f-string references."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node.value.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str) and isinstance(node.target, ast.Name):
            out[node.target.id] = node.value.value
    return out


def _text_of(node: ast.expr, consts: dict[str, str]) -> str | None:
    """The human-readable text of a message expression, or None when it is not a literal.

    A non-literal (a variable, a function call, a conditional) is skipped rather than
    failed: its text lives elsewhere and this guard would only be guessing.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for piece in node.values:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                parts.append(piece.value)
            elif isinstance(piece, ast.FormattedValue) and isinstance(piece.value, ast.Name):
                parts.append(consts.get(piece.value.id, ""))   # resolve the constant
        return "".join(parts)
    return None


def _message_expressions(tree: ast.Module) -> list[ast.expr]:
    """Every expression in *tree* whose text the owner ends up reading.

    Three constructions, because the first version of this guard only knew about the first
    and shipped green while six English strings sat in the second — including the one on the
    owner's own production dashboard (``trend_reason = "no ledger events"``):

    1. ``Issue(message=...)`` / ``error_body(code, message)`` — validation + API errors.
    2. ``*_reason = "..."`` — these are wired straight to ``freshness.*_unavailable_reason``
       and rendered by the XIRR badge and the trend empty state (``web/app.js``,
       ``web/charts.js``). A name ending in ``_reason`` is display text here, not a code.
    3. ``raise KeyError("...")`` inside ``portfolio/dashboard.py`` — that module catches its
       own KeyError and puts ``str(exc)`` on the wire as the reason, so those exception
       texts are display strings wearing an exception's clothes.
    """
    out: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "Issue":
                out += [kw.value for kw in node.keywords if kw.arg == "message"]
            elif name == "error_body":
                if len(node.args) >= 2:          # error_body(code, message, ...)
                    out.append(node.args[1])
                out += [kw.value for kw in node.keywords if kw.arg == "message"]
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                nm = getattr(t, "id", None) or getattr(t, "attr", None)
                if nm and nm.endswith("_reason") and node.value is not None:
                    out.append(node.value)
    return out


def _key_error_texts(tree: ast.Module) -> list[ast.expr]:
    """``raise KeyError(<literal>)`` — display text in dashboard.py only (see above)."""
    out: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) \
                and getattr(node.exc.func, "id", None) == "KeyError" and node.exc.args:
            out.append(node.exc.args[0])
    return out


def _scan(tree: ast.Module, *, include_key_errors: bool = False) -> list[tuple[int, str]]:
    """(lineno, text) for every offending message in *tree*. One filter, used by the real
    check AND by the detection-power test below — a self-test against a COPY of the filter
    proves only that the copy works."""
    consts = _module_constants(tree)
    targets = _message_expressions(tree)
    if include_key_errors:
        targets += _key_error_texts(tree)
    found: list[tuple[int, str]] = []
    for target in targets:
        text = _text_of(target, consts)
        if text is None or not text.strip() or text in _ALLOWED:
            continue
        if _CODE_SHAPED.match(text.strip()):     # a code, not display text (see above)
            continue
        if not _CJK.search(text):
            found.append((target.lineno, text))
    return found


def _offenders() -> list[str]:
    """(file:line, text) for every user-facing message literal with no Chinese in it."""
    found: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        is_dashboard = path.name == "dashboard.py" and path.parent.name == "portfolio"
        for lineno, text in _scan(tree, include_key_errors=is_dashboard):
            rel = path.relative_to(PACKAGE_ROOT.parent)
            found.append(f"{rel}:{lineno}  {text[:70]!r}")
    return found


def test_no_user_facing_message_is_english() -> None:
    offenders = _offenders()
    assert not offenders, (
        "user-facing messages must be Traditional Chinese — the frontend renders them "
        "verbatim:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_can_actually_fail() -> None:
    """Detection power: a guard that has never failed proves nothing.

    Every line below is a case this guard got wrong at some point today — the English
    message it must catch, the f-string whose Chinese lives in a module constant, the
    snake_case code that is deliberately English, the reason string that the FIRST version
    of this guard sailed straight past, and the non-literal it must skip rather than guess.
    """
    src = (
        'MSG = "已有交易紀錄，不可刪除"\n'
        'a = Issue(kind="x", message="quantity must be > 0")\n'       # caught
        'b = Issue(kind="x", message="股數必須大於 0")\n'              # not — zh
        'c = error_body("not_found", f"{sym} {MSG}", field="s")\n'    # not — zh in constant
        'd = error_body("bad", "alias is required")\n'                # caught
        'e = Issue(kind="x", message=some_variable)\n'                # not — not a literal
        'trend_reason = "no ledger events"\n'                         # caught (rule 2)
        'stop_reason = "budget_exhausted_mid_run"\n'                  # not — code-shaped
        'xirr_reason = "觀察期不足"\n'                                 # not — zh
    )
    caught = sorted(text for _, text in _scan(ast.parse(src)))
    assert caught == ["alias is required", "no ledger events", "quantity must be > 0"], caught


def test_the_key_error_rule_is_scoped_to_dashboard() -> None:
    """``raise KeyError`` is display text ONLY where dashboard.py puts str(exc) on the wire.
    Everywhere else a KeyError is an ordinary developer-facing exception and must not be
    dragged into a translation rule."""
    src = 'raise KeyError("no FX rate stored for USD/TWD")\n'
    tree = ast.parse(src)
    assert _scan(tree, include_key_errors=True), "dashboard scope must catch it"
    assert not _scan(tree, include_key_errors=False), "other modules must be left alone"
