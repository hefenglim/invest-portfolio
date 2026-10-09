"""DEF-023: the account-reference seam — backend writes a token, the fetch layer resolves it.

The backend has no Traditional-Chinese account name (``web/names.js`` is the one naming
authority, FU-D37 / G-01), yet its user-visible sentences named accounts anyway — by the
English ``accounts.name`` (「TW Broker」, DEF-008) or by the bare id (「tw_broker」 in the
XIRR badge, D-11). ``portfolio_dash/shared/account_ref.py`` ends the class: a sentence
embeds ``{account:<id>}`` and ``web/api.js`` swaps it for ``pdNames.account(id)`` on EVERY
response. Three guards, one per side of the contract:

(a) backend static — no f-string message embeds ``account.name`` (or a bare
    ``account_id``) outside the allowlist; every deferred site is named in ``_PENDING``
    and can only ever be removed from it;
(b) frontend static + behavioural — ``api.js`` resolves on BOTH the 2xx path and the
    error path, ``names.js`` owns ``resolveRefs``, the two regex literals equal the
    backend's grammar, and a node run of the real files proves the swap (and the id
    fallback without ``names.js``, and that money strings are untouched);
(c) wire — a token in a 200 body and in a 422 envelope reaches the client UNRESOLVED:
    the backend never resolves, so a page that bypassed ``pdApi`` would show the token,
    which is the visible failure this design prefers to a second spelling.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import upsert_instrument, upsert_opening
from portfolio_dash.shared.account_ref import (
    ACCOUNT_REF_RE,
    account_ref,
    account_refs_in,
    resolve_account_refs,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import DashboardClientFactory

_ROOT = Path(__file__).resolve().parents[2]
_PKG = _ROOT / "portfolio_dash"
_WEB = _ROOT / "web"

# ---------------------------------------------------------------------------------------
# (a) backend static guard
# ---------------------------------------------------------------------------------------

#: `{a.name}` / `{acct.name}` / `{account.name}` inside an f-string — the English label.
_NAME_EMBED = re.compile(r"^(?:a|acc|acct|account)\.name$")
#: `{account_id}` / `{x.account_id}` inside an f-string — the bare id. Widened 2026-09-23
#: (DEF-008 wave): the id also travels under other names — `{account}`, `{body.account}`,
#: `{acct}`, `{acct_id}` — and the first version of this guard, matching only
#: `account_id`, was blind to all of them (12 sites, measured).
_ID_EMBED = re.compile(r"(?:^|\.)(?:account_id|account|acct|acct_id)$")
#: The same English label reaching a wire field INDIRECTLY: ``account_name=<x>.name`` in a
#: call, or a parameter named ``*acct_name`` / ``*account_name`` defaulting to ``<x>.name``
#: (the inbox's ``_mk(_acct_name=account.name)`` — no f-string, so the first scanner could
#: not see it; the handover named it by line).
_NAME_FIELD = re.compile(r"(?:acct|account)_name$")
#: Formatting wrappers that pass their argument straight into the text (DEF-045, 2026-09-24).
#: The first scanner matched the WHOLE interpolated expression, so ``{_esc(account_id)}`` —
#: the cash-statement report's 「帳戶 TW Broker（tw_broker）」 header — was invisible to it.
#: A wrapper is unwrapped before matching; ``account_ref(...)`` is not a wrapper, it is the fix.
_PASS_THROUGH = frozenset({"_esc", "str", "escape", "html.escape", "_s"})
#: The accounts registry reader. DEF-045's second blind spot: the 帳本報告 printed 「TW Broker」
#: through ``{a.account_id: a.name for a in list_accounts(conn)}`` and a ``.get()`` lookup —
#: the English label never passed through an f-string, so no f-string rule could see it. Any
#: ``.name`` read off a row this call yields is now a finding (``<file>:list_accounts→<x>.name``).
_REGISTRY_CALL = "list_accounts"

#: Sites that may embed the id or the name, keyed ``"<file>:<expression>"``, each with the
#: reason it is not a user-facing sentence about an account.
_ALLOWED: dict[str, str] = {
    "shared/account_ref.py:account_id":
        "the token builder itself",
    "data_ingestion/holdings.py:account_id":
        "_DepthCapped's internal exception text, caught inside the walk, never rendered",
    "data_ingestion/store.py:account_id":
        "ledger_audit row labels (the audit trail's key, not a sentence)",
    "api/dividend_inbox.py:account_id":
        "inbox fingerprint keys (`div:<acct>:<sym>:<date>`), never rendered",
    "data_ingestion/config_seed.py:a.account_id":
        "seed-time ValueError for an unmapped settlement ccy — developer-facing, English",
    "data_ingestion/rules_binding.py:account_id":
        "same seed-time ValueError, the binding resolver's copy",
    "data_ingestion/validate.py:account_id":
        "unknown_account_message 「帳戶 X 不存在」: the id IS the thing that does not exist, "
        "so no display name can exist for it and the token would resolve to the id anyway",
    "data_ingestion/agents.py:a.name":
        "the LLM prompt's account roster (`id=name (ccy)`), read by the model, not the owner",
    "data_ingestion/agents.py:a.account_id":
        "same roster line",
    "export/cash_statement.py:account":
        "export FILE NAMES and the CSV provenance footer (`account=<id>, ccy=…`) — "
        "machine-readable identifiers, not a sentence",
    "ops/notify.py:account_id":
        "the PUSH text's account label 「帳戶 <id>」 (I-16): a push leaves through an external "
        "channel where no fetch layer resolves a token, and the backend has no zh name — so "
        "the account is named explicitly as an account, by id, never by the English name",
    "strategy/alerts.py:acct_id":
        "the alert's stable id key (`fx_drift:<acct>`), never rendered — its title uses the "
        "display name",
    # --- `.name` read off list_accounts() rows (DEF-045 detector) ---------------------------
    "api/routers/accounts.py:list_accounts→a.name":
        "GET /api/accounts wire field `name` (the stored registry record); web/ reads only "
        "`account_id` from it and names it through pdNames (ledger.js loadAccounts, "
        "corp-action-form.js) — test_account_name_single_source pins the frontend side",
    "api/routers/ledgers.py:list_accounts→a.name":
        "the ledger rows' wire field `account` (API compatibility); every ledger table renders "
        "`account_id` through pdNames (G-01, test_account_name_single_source)",
    "api/routers/symbol.py:list_accounts→a.name":
        "the drawer activity rows' wire field `account`; detail.js renders `account_id` "
        "through acctZh (G-01, test_account_name_single_source)",
    "data_ingestion/agents.py:list_accounts→a.name":
        "the LLM prompt's account roster (`id=name (ccy)`), read by the model, not the owner",
}

#: Sites still embedding a name or a bare id in a user-facing sentence, deferred to a later
#: wave. ⚠ NOT a permanent exemption: fixing one only ever REMOVES an entry, and
#: `test_the_pending_and_allow_lists_are_not_stale` fails on a stale one. EMPTY since
#: 2026-09-23 (DEF-008, Agent E): the overdraft / 換匯 / negative_cash sentences share
#: ``validate.cash_dip_sentence``, the four OversellError copies share
#: ``shared/oversold.py``, the 「帳戶 X 不存在」 envelopes call ``unknown_account_message``
#: (allowed above: the id IS the missing thing), and the rest embed ``account_ref``.
_PENDING: dict[str, str] = {}


def _unwrap(expr: ast.expr) -> ast.expr:
    """Strip pass-through formatting calls: ``_esc(x.account_id)`` is ``x.account_id``."""
    while (isinstance(expr, ast.Call) and len(expr.args) == 1 and not expr.keywords
           and ast.unparse(expr.func) in _PASS_THROUGH):
        expr = expr.args[0]
    return expr


def _registry_name_reads(tree: ast.AST) -> list[tuple[str, int]]:
    """``<x>.name`` where ``x`` iterates ``list_accounts(...)`` (a comprehension or a for)."""
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        loops: list[tuple[ast.expr, ast.expr, ast.AST]] = []
        if isinstance(node, ast.DictComp | ast.ListComp | ast.SetComp | ast.GeneratorExp):
            loops = [(g.target, g.iter, node) for g in node.generators]
        elif isinstance(node, ast.For):
            loops = [(node.target, node.iter, node)]
        for target, it, scope in loops:
            if not (isinstance(target, ast.Name) and isinstance(it, ast.Call)
                    and ast.unparse(it.func).split(".")[-1] == _REGISTRY_CALL):
                continue
            for sub in ast.walk(scope):
                if (isinstance(sub, ast.Attribute) and sub.attr == "name"
                        and isinstance(sub.value, ast.Name) and sub.value.id == target.id):
                    out.append((f"{_REGISTRY_CALL}→{target.id}.name", sub.lineno))
    return out


def _embeds() -> dict[str, list[int]]:
    """Every f-string interpolation of an account name or id, as ``file:expr`` -> lines."""
    found: dict[str, list[int]] = {}
    for path in sorted(_PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(_PKG).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            for part in node.values:
                if not isinstance(part, ast.FormattedValue):
                    continue
                raw = ast.unparse(part.value)
                expr = ast.unparse(_unwrap(part.value))
                if _NAME_EMBED.match(expr) or _ID_EMBED.search(expr):
                    found.setdefault(f"{rel}:{raw}", []).append(part.lineno)
        for key, line in _registry_name_reads(tree):
            found.setdefault(f"{rel}:{key}", []).append(line)
        for node in ast.walk(tree):
            pairs: list[tuple[str, ast.expr]] = []
            if isinstance(node, ast.Call):
                pairs = [(kw.arg, kw.value) for kw in node.keywords if kw.arg]
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                args = node.args.posonlyargs + node.args.args
                pairs = [(a.arg, d) for a, d in zip(args[len(args) - len(node.args.defaults):],
                                                    node.args.defaults, strict=True)]
                pairs += [(a.arg, d) for a, d in zip(node.args.kwonlyargs,
                                                     node.args.kw_defaults, strict=True)
                          if d is not None]
            for name, value in pairs:
                expr = ast.unparse(value)
                if _NAME_FIELD.search(name) and expr.endswith(".name"):
                    found.setdefault(f"{rel}:{name}={expr}", []).append(value.lineno)
    return found


def test_no_backend_sentence_embeds_an_account_name_or_bare_id_outside_the_lists() -> None:
    unexplained = {k: v for k, v in _embeds().items() if k not in _ALLOWED and k not in _PENDING}
    assert not unexplained, (
        "a backend f-string names an account by its English name or bare id — use "
        "portfolio_dash.shared.account_ref.account_ref(account_id) (the fetch layer resolves "
        "the token), or add the site to _ALLOWED with a reason:\n"
        f"{json.dumps(unexplained, indent=2)}"
    )


def test_the_pending_and_allow_lists_are_not_stale() -> None:
    present = set(_embeds())
    assert not (set(_PENDING) - present), f"fixed — remove from _PENDING: {set(_PENDING) - present}"
    assert not (set(_ALLOWED) - present), f"gone — remove from _ALLOWED: {set(_ALLOWED) - present}"
    assert not (set(_PENDING) & set(_ALLOWED))


def test_the_guard_bites() -> None:
    """A scanner that never matches would pass the test above with an empty list."""
    src = 'msg = f"出金超過 {account.name} 的餘額 {account_id}"\n'
    hits = [
        ast.unparse(p.value)
        for n in ast.walk(ast.parse(src)) if isinstance(n, ast.JoinedStr)
        for p in n.values if isinstance(p, ast.FormattedValue)
    ]
    assert [bool(_NAME_EMBED.match(h) or _ID_EMBED.search(h)) for h in hits] == [True, True]
    # …and the spellings the first version missed (DEF-008 wave, 2026-09-23).
    for expr in ("account", "body.account", "acct", "acct_id"):
        assert _ID_EMBED.search(expr), expr
    for expr in ("account_ref(account_id)", "names.get(acct_id, acct_id)", "accounts"):
        assert not _ID_EMBED.search(expr), expr
    assert _NAME_FIELD.search("_acct_name") and _NAME_FIELD.search("account_name")
    # DEF-045: a wrapped id, and a label read off the registry with no f-string at all.
    wrapped = ast.parse('h = f"帳戶 {_esc(acct_name)}（{_esc(account_id)}）"').body[0]
    parts = [p for p in ast.walk(wrapped) if isinstance(p, ast.FormattedValue)]
    assert [ast.unparse(_unwrap(p.value)) for p in parts] == ["acct_name", "account_id"]
    assert _ID_EMBED.search(ast.unparse(_unwrap(parts[1].value)))
    assert not _ID_EMBED.search(ast.unparse(_unwrap(
        ast.parse("_esc(account_ref(account_id))", mode="eval").body)))
    reads = _registry_name_reads(ast.parse("\n".join([
        "accts = {a.account_id: a.name for a in list_accounts(conn)}",
        "for acct in store.list_accounts(conn):",
        "    label = acct.name",
        "ok = {a.account_id for a in list_accounts(conn)}",
    ])))
    assert sorted(k for k, _ in reads) == ["list_accounts→a.name", "list_accounts→acct.name"]


def test_the_shared_helpers_own_the_grammar() -> None:
    assert account_ref("tw_broker") == "{account:tw_broker}"
    text = f"2884（{account_ref('tw_broker')}・2026-03-15）與 {account_ref('schwab')}"
    assert account_refs_in(text) == ["tw_broker", "schwab"]
    assert resolve_account_refs(text, lambda i: {"tw_broker": "台灣券商"}.get(i, i)) == \
        "2884（台灣券商・2026-03-15）與 schwab"
    assert resolve_account_refs("1,234.50", str.upper) == "1,234.50"


# ---------------------------------------------------------------------------------------
# (b) frontend — static + a node run of the real files
# ---------------------------------------------------------------------------------------

_JS_GRAMMAR = r"/\{account:([^{}\s]+)\}/g"


def test_api_js_resolves_on_both_paths_and_names_js_owns_the_resolver() -> None:
    api = (_WEB / "api.js").read_text(encoding="utf-8")
    names = (_WEB / "names.js").read_text(encoding="utf-8")
    # 2xx: the parsed body goes through the walk before it is returned.
    assert "return _resolveRefs(JSON.parse(text));" in api
    # error envelope: message and issues alike.
    assert "_resolveRefs(message)" in api and "_resolveRefs(issues)" in api
    assert "names.resolveRefs(s)" in api
    assert "resolveRefs(text)" in names
    # One grammar, three owners: the JS literal in both files equals the Python regex.
    assert _JS_GRAMMAR in api and _JS_GRAMMAR in names
    assert _JS_GRAMMAR == "/" + ACCOUNT_REF_RE.pattern + "/g"


_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const apiSrc = fs.readFileSync(process.argv[2], 'utf8');
const namesSrc = fs.readFileSync(process.argv[3], 'utf8');

function response(status, body) {
  return {
    ok: status >= 200 && status < 300, status: status, statusText: 'x',
    headers: { get: function () { return ''; } },
    text: async function () { return body; },
    json: async function () { return JSON.parse(body); },
  };
}
function sandbox(withNames) {
  const errors = [];
  const sb = {
    window: { location: { pathname: '/index.html', replace: function () {} } },
    document: { dispatchEvent: function () {}, createElement: function () { return {}; },
                body: { appendChild: function () {} } },
    CustomEvent: function (type, init) { this.type = type; this.detail = init && init.detail; },
    AbortController: function () { this.signal = {}; this.abort = function () {}; },
    URLSearchParams: URLSearchParams, URL: URL, setTimeout: setTimeout,
    console: { error: function () { errors.push([].slice.call(arguments).join(' ')); },
               warn: console.warn, log: console.log },
    fetch: null,
  };
  sb.globalThis = sb;
  sb.consoleErrors = errors;
  vm.createContext(sb);
  // The page order (DEF-044): names.js BEFORE api.js, exactly as every web/*.html loads them.
  if (withNames) vm.runInContext(namesSrc, sb);
  vm.runInContext(apiSrc, sb);
  return sb;
}
const OK = JSON.stringify({
  kpis: { xirr_reason: '帳本中有 1 筆公司行動無法套用：2884（{account:tw_broker}・2026-03-15）',
          total_value: '1234567.89', n: 5, nothing: null, flag: true },
  rows: ['{account:schwab}', { reason: '{account:moomoo_my} 與 {account:nope}' }],
});
const ERR = JSON.stringify({ error: { code: 'oversell_unacknowledged',
  message: '需確認賣超（{account:tw_broker}）', field: 'shares',
  issues: [{ sev: 'warn', code: 'trade_before_opening',
             text: '交易日早於 8299 在 {account:tw_broker} 的期初庫存建檔日' }] } });

(async function () {
  const out = {};
  for (const withNames of [true, false]) {
    const sb = sandbox(withNames);
    sb.fetch = async function () { return response(200, OK); };
    const body = await sb.window.pdApi.get('/api/dashboard');
    sb.fetch = async function () { return response(422, ERR); };
    let err = null;
    try { await sb.window.pdApi.post('/api/input/manual/commit', {}); } catch (e) { err = e; }
    out[withNames ? 'with_names' : 'without_names'] = {
      body: body,
      err: { message: err.message, code: err.code, field: err.field, issues: err.issues },
      console_errors: sb.consoleErrors,
    };
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _node() -> Path | None:
    import playwright

    for name in ("node.exe", "node"):
        node = Path(playwright.__file__).parent / "driver" / name
        if node.exists():
            return node
    return None


@pytest.fixture(scope="module")
def resolved(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, object]]:
    node = _node()
    if node is None:
        pytest.skip("Playwright's bundled node is not installed in this venv")
    harness = tmp_path_factory.mktemp("account_ref") / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [str(node), str(harness), str(_WEB / "api.js"), str(_WEB / "names.js")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}"
    out: dict[str, dict[str, object]] = json.loads(proc.stdout)
    return out


def test_the_fetch_layer_resolves_tokens_in_the_2xx_body(
    resolved: dict[str, dict[str, object]],
) -> None:
    body = resolved["with_names"]["body"]
    assert isinstance(body, dict)
    kpis = body["kpis"]
    assert kpis["xirr_reason"] == "帳本中有 1 筆公司行動無法套用：2884（台灣券商・2026-03-15）"
    # Money strings, counts, null and booleans are exactly what the server sent.
    assert kpis["total_value"] == "1234567.89" and kpis["n"] == 5
    assert kpis["nothing"] is None and kpis["flag"] is True
    assert body["rows"][0] == "嘉信 Schwab"
    assert body["rows"][1] == {"reason": "Moomoo MY 與 nope"}   # unknown id -> the id


def test_the_fetch_layer_resolves_tokens_in_the_error_envelope(
    resolved: dict[str, dict[str, object]],
) -> None:
    err = resolved["with_names"]["err"]
    assert isinstance(err, dict)
    assert err["message"] == "需確認賣超（台灣券商）" and err["code"] == "oversell_unacknowledged"
    assert err["issues"] == [{"sev": "warn", "code": "trade_before_opening",
                              "text": "交易日早於 8299 在 台灣券商 的期初庫存建檔日"}]


def test_without_names_js_the_id_itself_is_shown(
    resolved: dict[str, dict[str, object]],
) -> None:
    """A page that breaks the load order degrades to the id, never to a raw token and never
    to a crash — and (DEF-044) says so LOUDLY: api.js reports the missing names.js at load.
    Five pages shipped without names.js and the silent id fallback hid it for a round."""
    body = resolved["without_names"]["body"]
    err = resolved["without_names"]["err"]
    assert isinstance(body, dict) and isinstance(err, dict)
    assert body["kpis"]["xirr_reason"].endswith("2884（tw_broker・2026-03-15）")
    assert body["rows"][0] == "schwab"
    assert err["message"] == "需確認賣超（tw_broker）"
    assert "{account:" not in json.dumps(resolved, ensure_ascii=False)
    errors = resolved["without_names"]["console_errors"]
    assert isinstance(errors, list) and len(errors) == 1, errors
    loud = errors[0]
    assert "names.js must be loaded before api.js" in str(loud)
    # …and a page with the right order is silent: the smoke e2e counts console errors.
    assert resolved["with_names"]["console_errors"] == []


# ---------------------------------------------------------------------------------------
# (c) wire — the backend never resolves
# ---------------------------------------------------------------------------------------

def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="8299", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Tech", name="群聯"))
    upsert_opening(conn, account_id="tw_broker", symbol="8299", shares=Decimal("500"),
                   original_cost_total=Decimal("200000"), build_date=date(2026, 7, 21))


def test_a_token_reaches_the_client_unresolved_in_a_200_and_in_a_422(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client: TestClient = dashboard_client_factory(_seed)
    preview = client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "8299", "side": "buy",
        "date": "2026-07-01", "shares": "100", "price": "45.20"})
    assert preview.status_code == 200
    texts = [i["text"] for i in preview.json()["issues"] if i["code"] == "trade_before_opening"]
    assert texts and "{account:tw_broker}" in texts[0]
    assert "台灣券商" not in preview.text and "TW Broker" not in preview.text

    refused = client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "8299", "side": "sell",
        "date": "2026-07-01", "shares": "600", "price": "45.20"})
    assert refused.status_code == 422, refused.text
    err = refused.json()["error"]
    assert err["code"] == "oversell_unacknowledged"
    assert any("{account:tw_broker}" in i["text"] for i in err["issues"])
    assert "台灣券商" not in refused.text and "TW Broker" not in refused.text
