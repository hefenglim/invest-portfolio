"""DEF-053 (2026-09-25): text the owner reads never names a task scope by its identifier.

R3 developer report, verifier-confirmed: 洞察管線 › 新增洞察任務 → 觸發「預警觸發」→ 建立 → the
toast's sub-line read 「…：on_alert 任務預設停用，確認監聽規則後再啟用」
(``web/pipeline-wizard.js:374``) — the wizard's own button says 「預警觸發」. The class scan
found seven more strings with a CJK body and a scope identifier: two more in ``web/`` (the
略過原因 label, the 數據變數總表 footnote) and five in the backend (four user-facing, one
registry-only — whitelisted below) — including the R1 gate, which also INTERPOLATED the raw
scope (「per_symbol 變數 {{symbol}} 不可用於 portfolio 範圍組合」): that ``portfolio`` half no
literal scan can see, which is why the behavioural tests read the served message.

Two guards, both about text that reaches the user, never about code identifiers:

* **behavioural** — the R1 message a real dry-run returns, the 400 a real on_alert schedule
  request returns, and the variable description ``GET /api/prompt-vars`` serves, each read
  through the router;
* **class ban** — every user-facing string: a JS/HTML literal or a Python string constant
  (docstrings excluded) that carries CJK text. Code identifiers in this repo are ASCII, so
  ``d.scope === 'on_alert'`` is never flagged while 「：on_alert 任務預設停用」 always is. The
  scanner is proven to bite on the exact pre-fix lines and to stay silent on code.

The e2e half (the wizard's toast in a real browser) is
``tests/e2e/test_def053_wizard_toast_flow.py``.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parents[2]
_WEB = _ROOT / "web"
_PKG = _ROOT / "portfolio_dash"

IDENTIFIERS = ("on_alert", "per_symbol", "per_market", "portfolio", "all_registered", "custom")
_IDENT = re.compile(
    r"(?<![A-Za-z0-9_\-.])(" + "|".join(IDENTIFIERS) + r")(?![A-Za-z0-9_\-])")
_CJK = re.compile(r"[㐀-鿿＀-￯]")

# (file, literal-prefix) → why it is not user-facing. Keep this list SHORT and argued.
_WHITELIST: dict[tuple[str, str], str] = {
    ("official_templates.py", "洞察卡預警附加守則"): (
        "PROMPT_REGISTRY 'feature' metadata — enumerated by tests/llm_insight/"
        "test_prompt_registry.py only, never served or rendered"),
}


def user_facing_hit(text: str) -> str | None:
    """The scope identifier a USER-FACING string names, or None. User-facing = carries CJK."""
    if not _CJK.search(text):
        return None
    m = _IDENT.search(text)
    return m.group(1) if m else None


def _js_literals(src: str) -> list[tuple[int, str]]:
    """(offset, body) of every '…' / "…" / `…` literal, comments skipped."""
    out: list[tuple[int, str]] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "'\"`":
            j, buf = i + 1, []
            while j < n and src[j] != c:
                if src[j] == "\\":
                    buf.append(src[j:j + 2])
                    j += 2
                    continue
                if c != "`" and src[j] == "\n":
                    break
                buf.append(src[j])
                j += 1
            out.append((i, "".join(buf)))
            i = j + 1
            continue
        i += 1
    return out


def _web_hits() -> list[str]:
    hits: list[str] = []
    for f in sorted([*_WEB.glob("*.js"), *_WEB.glob("*.html")]):
        if f.name.endswith(".min.js"):
            continue
        src = f.read_text(encoding="utf-8", errors="replace")
        chunks = [(0, src)]
        if f.suffix == ".html":
            chunks = [(m.start(1), m.group(1)) for m in re.finditer(
                r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", src, re.S)]
            body = re.sub(r"<script.*?</script>|<style.*?</style>|<!--.*?-->", "", src,
                          flags=re.S)
            for m in re.finditer(r">([^<]+)<|(?:title|placeholder|aria-label)=\"([^\"]*)\"",
                                 body):
                t = m.group(1) or m.group(2) or ""
                if user_facing_hit(t):
                    hits.append(f"{f.name}: {t.strip()[:80]}")
        for off, chunk in chunks:
            for pos, lit in _js_literals(chunk):
                if user_facing_hit(lit):
                    line = src.count("\n", 0, off + pos) + 1
                    hits.append(f"{f.name}:{line}: {lit[:80]}")
    return hits


def _py_hits() -> list[str]:
    hits: list[str] = []
    for f in sorted(_PKG.rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        docs = {
            id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and n.body and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docs and user_facing_hit(node.value)):
                if any(f.name == wf and node.value.startswith(pre)
                       for (wf, pre) in _WHITELIST):
                    continue
                hits.append(f"{f.relative_to(_ROOT)}:{node.lineno}: {node.value[:80]}")
    return hits


# --- the scanner bites, and only on user text -------------------------------------------


def test_the_scanner_flags_the_exact_pre_fix_strings() -> None:
    assert user_facing_hit("：on_alert 任務預設停用，確認監聽規則後再啟用") == "on_alert"
    assert user_facing_hit("範圍不相容（模板含 per_symbol 變數，任務非單一標的）") == "per_symbol"
    assert user_facing_hit("per_symbol 變數 {{symbol}} 不可用於 portfolio 範圍組合") is not None
    js = "window.toast('已建立洞察任務', 'ok', name + '：on_alert 任務預設停用');"
    assert [user_facing_hit(b) for _o, b in _js_literals(js)] == [None, None, "on_alert"]


def test_the_scanner_never_flags_code_identifiers() -> None:
    js = ("if (d.scope === 'on_alert') { mode = 'all_registered'; }  // on_alert 註解\n"
          "var SCOPE = { per_symbol: '單一標的', portfolio: '全組合' };")
    assert [b for _o, b in _js_literals(js) if user_facing_hit(b)] == []
    assert user_facing_hit("portfolio-dash") is None and user_facing_hit("on_alert") is None


# --- the class ban ------------------------------------------------------------------------


def test_no_user_facing_web_string_names_a_scope_identifier() -> None:
    hits = _web_hits()
    assert not hits, "user-facing web text names a scope identifier:\n" + "\n".join(hits)


def test_no_user_facing_backend_string_names_a_scope_identifier() -> None:
    hits = _py_hits()
    assert not hits, "user-facing backend text names a scope identifier:\n" + "\n".join(hits)


# --- behavioural: the strings the routers actually serve -------------------------------------


def test_the_r1_dry_run_message_names_scopes_in_words(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """R1 interpolated the raw scope — only a real dry run shows what it prints."""
    sp = api_client.post("/api/strategy-prompts",
                         json={"name": "個股", "body": "{{symbol_detail_json}}"}).json()
    # the wizard's dry run of an unsaved draft: a portfolio task with a per-symbol template
    pf = api_client.post("/api/insight-tasks/0/preflight", json={
        "name": "組合", "scope": "portfolio", "strategy_ids": [sp["id"]]})
    assert pf.status_code == 200, pf.text
    r1 = next(g for g in pf.json()["gates"] if g["id"] == "R1")
    assert "「單一標的」變數" in r1["msg"] and "「全組合」" in r1["msg"], r1
    assert user_facing_hit(r1["msg"]) is None, r1
    # a saved task whose template LATER gained a per-symbol variable: the pipeline card's node
    ok = api_client.post("/api/strategy-prompts", json={"name": "組合", "body": "看全局"}).json()
    it = api_client.post("/api/insight-types", json={
        "name": "組合", "scope": "portfolio", "strategy_ids": [ok["id"]]}).json()
    golden_db.execute("UPDATE strategy_prompts SET body = '{{symbol_detail_json}}' WHERE id = ?",
                      (ok["id"],))
    golden_db.commit()
    task = next(t for t in api_client.get("/api/insight-tasks/status").json()["tasks"]
                if t["id"] == it["id"])
    assert "「單一標的」變數" in str(task["nodes"]), task["nodes"]
    assert user_facing_hit(str(task["nodes"])) is None, task["nodes"]


def test_the_on_alert_schedule_refusal_and_the_variable_help_read_in_words(
    api_client: TestClient,
) -> None:
    it = api_client.post("/api/insight-types", json={
        "name": "預警", "scope": "on_alert", "strategy_ids": []}).json()
    r = api_client.post(f"/api/insight-types/{it['id']}/schedule", json={"cron": "0 8 * * *"})
    assert r.status_code == 400
    assert "預警觸發" in r.json()["error"]["message"]
    assert user_facing_hit(r.json()["error"]["message"]) is None
    for v in api_client.get("/api/prompt-vars").json():
        assert user_facing_hit(str(v.get("description") or "")) is None, v
