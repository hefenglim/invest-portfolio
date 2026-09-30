"""Post-closure item 13 (owner 2026-09-30): a zh sentence never names an account by its raw id.

The verifier's R8 note, recorded since R1 and unchanged: deleting ONE leg of a multi-account
corporate action is refused with 「… 還有 moomoo_my 的同一筆事件 …」, and ``web/ledger.js``
prints that message inside the 「這筆行動屬於多帳戶整組紀錄」 confirm. The sentence is built in
``data_ingestion/validate.py::validate_corporate_action_change`` as::

    accounts = "、".join(sorted({s.account_id for s in set_rows}))

— the ids are JOINED into a local first, and only the local reaches the f-string. The DEF-023
guard (``test_account_ref_seam.py``) inspects the interpolated expression itself (``{accounts}``,
a plural it deliberately does not match), so it could not see an id that travelled through a
join, a comprehension or a local. The same shape sat in ``forex/fx_pnl.py::_rollup_reason``
(the dashboard's 「部分帳戶缺匯率已略過：moomoo_my（USD/MYR）」) and in
``export/holdings_report.py::_filter_label`` (``names.get(account, account)`` — the FALLBACK is
the raw id, printed for a filtered account that holds nothing).

The fix is the DEF-023 seam, unchanged: each sentence embeds ``account_ref(id)`` and the fetch
layer (``web/api.js``) resolves the token to the display name on every response, error
envelopes and downloaded text included. THIS file adds the guard that follows an id through a
local, a join, a comprehension, an ``IfExp`` / ``or`` branch, a ``+`` concatenation and a
``.get(key, fallback)`` fallback — scoped to user-facing zh sentences (an f-string or ``+``
chain with CJK text or full-width punctuation), because non-zh interpolations (keys, prompts,
developer errors) are the existing guard's business.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_transaction, list_corporate_actions
from portfolio_dash.forex.fx_pnl import _rollup_reason
from portfolio_dash.shared.account_ref import ACCOUNT_REF_RE, account_ref, resolve_account_refs
from portfolio_dash.shared.models.enums import Side

_PKG = Path(__file__).resolve().parents[2] / "portfolio_dash"
_BASE = "/api/ledgers/corporate-actions"
_IDS = ("tw_broker", "schwab", "moomoo_my")
#: The display names ``web/names.js`` owns — only used to prove what the page would read.
_NAMES = {"tw_broker": "台灣券商", "schwab": "嘉信 Schwab", "moomoo_my": "Moomoo MY"}


def _bare_ids(text: str) -> list[str]:
    """Account ids printed OUTSIDE a ``{account:<id>}`` token (the token is the right form)."""
    text = ACCOUNT_REF_RE.sub(" ", text)
    return [i for i in _IDS if re.search(rf"(?<![\w]){re.escape(i)}(?![\w])", text)]


def _two_leg_split(api_client: TestClient, golden_db: sqlite3.Connection) -> list[tuple[int, str]]:
    """AAPL held in schwab (golden) AND moomoo_my, split once -> a two-row set."""
    insert_transaction(golden_db, account_id="moomoo_my", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("20"), price=Decimal("90"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 12))
    golden_db.commit()
    r = api_client.post(_BASE, json={
        "account_id": "schwab", "date": "2026-06-10", "kind": "SPLIT",
        "from_symbol": "AAPL", "to_symbol": "AAPL", "ratio_to": "4", "ratio_from": "1"})
    assert r.status_code in (200, 201), r.text
    legs = [(a.id, a.account_id) for a in list_corporate_actions(golden_db)]
    assert sorted(a for _, a in legs) == ["moomoo_my", "schwab"]
    return legs


# ------------------------------------------------------------------ the reported instance


def test_deleting_one_leg_names_the_other_account_by_token(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The verifier's reproduction: the refusal the group-delete confirm prints verbatim."""
    legs = _two_leg_split(api_client, golden_db)
    (leg_id, leg_acct), (_, other) = legs[0], legs[1]
    r = api_client.delete(f"{_BASE}/{leg_id}")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "partial_action_set_change"
    msg = err["message"]
    assert account_ref(other) in msg, msg
    assert _bare_ids(msg) == [], msg
    # …and what the owner reads once api.js resolves the token.
    shown = resolve_account_refs(msg, lambda i: _NAMES.get(i, i))
    assert _NAMES[other] in shown and other not in shown, shown
    assert leg_acct not in shown   # the refused row's own account is not named at all
    assert len(list_corporate_actions(golden_db)) == 2   # still refused, nothing removed


def test_editing_one_leg_off_the_set_names_the_other_account_by_token(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The same sentence on the edit door (「只修改其中一筆」) — one builder, two verbs."""
    legs = _two_leg_split(api_client, golden_db)
    (leg_id, leg_acct), (_, other) = legs[0], legs[1]
    r = api_client.put(f"{_BASE}/{leg_id}", json={
        "account_id": leg_acct, "date": "2026-06-09", "kind": "SPLIT",
        "from_symbol": "AAPL", "to_symbol": "AAPL", "ratio_to": "4", "ratio_from": "1"})
    assert r.status_code == 422, r.text
    msg = r.json()["error"]["message"]
    assert "只修改其中一筆" in msg and account_ref(other) in msg, msg
    assert _bare_ids(msg) == [], msg


# ----------------------------------------------------------- the siblings the scan found


def test_the_fx_rollup_reason_names_the_skipped_account_by_token() -> None:
    """The dashboard FX card's 「部分帳戶缺匯率已略過」 note (``forex/fx_pnl.py``)."""
    reason = _rollup_reason({"moomoo_my": ["USD/MYR"]},
                            realized_partial=False, unrealized_partial=True)
    assert reason is not None
    assert f"{account_ref('moomoo_my')}（USD/MYR）" in reason, reason
    assert _bare_ids(reason) == [], reason


def test_the_holdings_report_filter_names_an_unheld_account_by_token(
    api_client: TestClient,
) -> None:
    """``_filter_label`` read the name off the HELD rows and fell back to the raw id — so a
    report filtered to an account that holds nothing (moomoo_my on the golden ledger) printed
    「篩選　帳戶 moomoo_my」. The fallback is now the token the download seam resolves."""
    r = api_client.post("/api/export/holdings-report", json={"account": "moomoo_my"})
    assert r.status_code == 200, r.text
    visible = re.sub(r"<[^>]+>", " ", re.sub(r"<style.*?</style>", " ",
                                             r.content.decode("utf-8-sig"), flags=re.S))
    assert "帳戶 {account:moomoo_my}" in visible
    assert _bare_ids(visible) == [], _bare_ids(visible)


# --------------------------------------------------------------------- the class guard

#: A name that carries an account id when it is the VALUE being printed.
_ID_LEAF = re.compile(r"^(?:account_id|account|acct|acct_id|aid|account_ids|acct_ids)$")
#: CJK ideographs, CJK punctuation and full-width forms: a user-facing zh sentence.
_ZH = re.compile(r"[一-鿿　-〿＀-￯]")
#: The seam and the one sentence that owns the raw id on purpose — never a carrier.
_SAFE_CALLS = frozenset({"account_ref", "unknown_account_message"})
#: Calls that pass their (first) argument's text straight through.
_WRAPPERS = frozenset({"sorted", "set", "list", "tuple", "str", "_esc", "escape", "repr",
                       "strip"})

#: ``file:function:expr`` -> why a raw id in this zh sentence is correct.
_ALLOWED: dict[str, str] = {
    "data_ingestion/validate.py:unknown_account_message:account_id":
        "「帳戶 X 不存在」: the id IS the thing that does not exist, so no display name exists "
        "and the token would resolve to the id anyway (DEF-023 decision, pinned by "
        "tests/data_ingestion/test_m2_unknown_account_message.py)",
    "ops/notify.py:_push_account_label:account_id":
        "the PUSH text's 「帳戶 <id>」 (I-16): a push leaves through an external channel where "
        "no fetch layer resolves a token, and the backend owns no zh name",
    "strategy/whatif.py:compute_whatif:resolved":
        "「未知帳戶 X」 fires only when the account row does not exist "
        "(rules_binding._account_row KeyError) — the unknown_account_message case again",
}


def _carries(e: ast.expr) -> bool:
    """Does the TEXT of *e* contain an account id (not wrapped in ``account_ref``)?"""
    if isinstance(e, ast.Name):
        return bool(_ID_LEAF.match(e.id))
    if isinstance(e, ast.Attribute):
        return bool(_ID_LEAF.match(e.attr))
    if isinstance(e, ast.Call):
        fn = ast.unparse(e.func).split(".")[-1]
        if fn in _SAFE_CALLS:
            return False
        if fn == "join" and e.args:
            return _carries(e.args[0])
        if fn in _WRAPPERS and e.args:
            return _carries(e.args[0])
        if fn == "get" and len(e.args) >= 2:   # names.get(id, id): the fallback IS printed
            return _carries(e.args[1])
        return False
    if isinstance(e, ast.ListComp | ast.SetComp | ast.GeneratorExp):
        return _carries(e.elt)
    if isinstance(e, ast.JoinedStr):
        return any(_carries(v.value) for v in e.values if isinstance(v, ast.FormattedValue))
    if isinstance(e, ast.IfExp):
        return _carries(e.body) or _carries(e.orelse)
    if isinstance(e, ast.BoolOp):
        return any(_carries(v) for v in e.values)
    if isinstance(e, ast.BinOp) and isinstance(e.op, ast.Add):
        return _carries(e.left) or _carries(e.right)
    return False


def _is_zh(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) \
        and bool(_ZH.search(node.value))


def _concat(node: ast.expr) -> list[ast.expr]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _concat(node.left) + _concat(node.right)
    return [node]


def _hits_in(tree: ast.Module, rel: str) -> dict[str, int]:
    """``file:function:expr`` -> line, for every id-carrying piece of a zh sentence."""
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    owner: dict[int, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for fn in functions:          # ast.walk is breadth-first: an inner def overwrites
        for n in ast.walk(fn):
            owner[id(n)] = fn

    def assigned(fn: ast.AST | None) -> dict[str, list[ast.expr]]:
        out: dict[str, list[ast.expr]] = {}
        for n in ast.walk(fn) if fn is not None else ():
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        out.setdefault(t.id, []).append(n.value)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) \
                    and n.value is not None:
                out.setdefault(n.target.id, []).append(n.value)
        return out

    found: dict[str, int] = {}
    for node in ast.walk(tree):
        pieces: list[ast.expr] = []
        if isinstance(node, ast.JoinedStr) and any(_is_zh(v) for v in node.values):
            pieces = [v.value for v in node.values if isinstance(v, ast.FormattedValue)]
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            parts = _concat(node)
            if any(_is_zh(p) for p in parts):
                pieces = [p for p in parts if not isinstance(p, ast.Constant)]
        if not pieces:
            continue
        enclosing = owner.get(id(node))
        locals_ = assigned(enclosing)
        for piece in pieces:
            carried = _carries(piece) or (
                isinstance(piece, ast.Name)
                and any(_carries(rhs) for rhs in locals_.get(piece.id, [])))
            if carried:
                where = enclosing.name if enclosing else "<module>"
                key = f"{rel}:{where}:{ast.unparse(piece)}"
                found.setdefault(key, piece.lineno)
    return found


def _scan() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in sorted(_PKG.rglob("*.py")):
        rel = path.relative_to(_PKG).as_posix()
        found.update(_hits_in(ast.parse(path.read_text(encoding="utf-8")), rel))
    return found


def test_no_zh_sentence_carries_an_account_id_outside_the_allow_list() -> None:
    unexplained = {k: v for k, v in _scan().items() if k not in _ALLOWED}
    assert not unexplained, (
        "a user-facing zh sentence carries a raw account id (through a local, a join, a "
        "fallback …) — embed portfolio_dash.shared.account_ref.account_ref(id) instead, or add "
        f"the site to _ALLOWED with the reason:\n{json.dumps(unexplained, indent=2)}"
    )


def test_the_allow_list_is_not_stale() -> None:
    gone = set(_ALLOWED) - set(_scan())
    assert not gone, f"no longer present — remove from _ALLOWED: {gone}"


def test_the_guard_bites() -> None:
    """Each shape the DEF-023 guard could not see is caught; the token form is not."""
    src = "\n".join([
        "def a(set_rows):",
        "    accounts = '、'.join(sorted({s.account_id for s in set_rows}))",
        "    return f'還有 {accounts} 的同一筆事件'",
        "def b(missing):",
        "    accounts = '、'.join(f'{aid}（x）' for aid in sorted(missing))",
        "    return f'部分帳戶缺匯率已略過：{accounts}'",
        "def c(names, account):",
        "    txt = '全部' if account is None else names.get(account, account)",
        "    return f'篩選　帳戶 {txt}'",
        "def d(acct):",
        "    return '帳戶 ' + acct + ' 餘額不足'",
        "def e(set_rows):",
        "    ids = sorted({s.account_id for s in set_rows})",
        "    accounts = '、'.join(account_ref(a) for a in ids)",
        "    return f'還有 {accounts} 的同一筆事件'",
        "def f(names, account):",
        "    return f'篩選　帳戶 {names.get(account, account_ref(account))}'",
        "def g(key):",
        "    return f'key={key}'",
    ])
    assert sorted(_hits_in(ast.parse(src), "x.py")) == [
        "x.py:a:accounts", "x.py:b:accounts", "x.py:b:aid", "x.py:c:txt", "x.py:d:acct"]
