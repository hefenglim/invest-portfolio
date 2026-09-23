"""I-11 (C-5) + I-14 (C-3): one app, one word per corporate-action kind — the server's.

I-14. ``web/detail.js`` kept ``ACTION_KIND_ZH = { SPLIT: '拆併股', …, SPINOFF: '分割' }`` while
the ledger (``shared/corporate_actions.py::KIND_ZH``) says SPLIT 「分割」, SPINOFF 「分拆」 — so
the drawer called a SPINOFF by the ledger's word for a SPLIT, and an e2e pinned 「拆併股 3：1」.
The scan found three more private copies (app.js's 未套用 banner, detail.js's second one for the
drawer's cause lines, input.js's CSV preview), which agreed today and would not have been
updated by anyone changing the ledger's word. Every wire that names a kind now carries
``kind_label`` (``shared.corporate_actions.kind_label``) and the pages print it.

I-11. The drawer's ``action_issues.unapplied`` rows lacked ``action_id`` (detail.js already
read it for the 前往公司行動帳本 link — ``undefined``, so the link fell back to the tuple) and
``kind_label``; the dashboard's ``unapplied_actions`` gains ``kind_label`` too.

Scan (2026-09-23): 6 frontend kind→zh tables — 4 removed (detail.js ×2, app.js, input.js),
2 allowlisted below with reasons (broker-import.js TYPE_ZH, ledger.js's edit-modal select),
whose labels are pinned equal to the server's by ``test_every_remaining_label_is_the_servers_word``.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_corporate_action
from portfolio_dash.shared.corporate_actions import KIND_ZH, CorporateActionKind, kind_label

_WEB = Path(__file__).resolve().parents[2] / "web"
#: `SPLIT: '分割'` / `['SPLIT', '分割']` — a kind paired with a zh label, in any web file.
_PAIR = re.compile(r"""\b(SPLIT|EXCHANGE|SPINOFF)['"]?\s*[:,]\s*['"]([^'"]*[一-鿿][^'"]*)['"]""")
#: Files allowed to carry literal kind labels, and why. Every label they carry must still
#: EQUAL the server's word (asserted below), so an allowlisted file cannot drift either.
_ALLOWED: dict[str, str] = {
    "broker-import.js": "TYPE_ZH labels broker-statement ROW types (REVERSE_SPLIT, NAME_CHANGE, "
                        "DRIP …) — a vocabulary wider than the ledger's three kinds, rendered "
                        "before any ledger row exists",
    "ledger.js": "the 公司行動 edit modal's kind <select> needs option labels BEFORE any "
                 "server row is chosen; its three labels are pinned equal to KIND_ZH here",
}


def _pairs() -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {}
    for path in sorted(_WEB.glob("*.js")):
        found = _PAIR.findall(path.read_text(encoding="utf-8"))
        if found:
            out[path.name] = found
    return out


def test_no_page_keeps_its_own_kind_table() -> None:
    offenders = {k: v for k, v in _pairs().items() if k not in _ALLOWED}
    assert not offenders, (
        "a web file names corporate-action kinds itself — print the wire's `kind_label` "
        f"(shared/corporate_actions.py::kind_label) instead: {offenders}")


def test_every_remaining_label_is_the_servers_word() -> None:
    for name, pairs in _pairs().items():
        for kind, label in pairs:
            assert label == KIND_ZH[kind], (name, kind, label, KIND_ZH[kind])


def test_the_allowlist_is_not_stale() -> None:
    assert set(_ALLOWED) <= set(_pairs())


def test_the_guard_bites() -> None:
    src = "const ACTION_KIND_ZH = { SPLIT: '拆併股', EXCHANGE: '換股', SPINOFF: '分割' };"
    assert _PAIR.findall(src) == [("SPLIT", "拆併股"), ("EXCHANGE", "換股"), ("SPINOFF", "分割")]


def test_the_helper_reads_like_the_ledger() -> None:
    assert kind_label("SPLIT") == "分割" and kind_label(" spinoff ") == "分拆"
    assert kind_label(CorporateActionKind.EXCHANGE) == "換股"
    assert kind_label("MERGER") == "MERGER"          # unknown: echoed, never guessed


# ----------------------------------------------------------------------------- the wire


def _unapplied(conn: sqlite3.Connection) -> int:
    """A SPINOFF on a symbol this account never held — E1, refused by the replay."""
    action_id = insert_corporate_action(
        conn, account_id="tw_broker", action_date=date(2026, 6, 10),
        kind=CorporateActionKind.SPINOFF, from_symbol="2330", to_symbol="2330",
        ratio_to=Decimal("1"), ratio_from=Decimal("2"), cost_carry=Decimal("0.1"))
    conn.execute("UPDATE corporate_actions SET account_id='schwab' WHERE id=?", (action_id,))
    conn.commit()
    return action_id


def test_the_dashboard_and_the_drawer_carry_the_kind_label_and_the_row_id(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    action_id = _unapplied(golden_db)
    dash = api_client.get("/api/dashboard").json()["unapplied_actions"]
    assert [(u["kind_label"], u["action_id"]) for u in dash] == [("分拆", action_id)]
    detail = api_client.get("/api/symbol/2330/detail").json()
    (u,) = detail["action_issues"]["unapplied"]
    assert u["kind_label"] == "分拆" and u["action_id"] == action_id
    rows = [a for a in detail["activity"] if a["side"] == "action"]
    assert rows and all(a["kind_label"] == "分拆" for a in rows)


def test_the_csv_preview_carries_the_kind_label(api_client: TestClient) -> None:
    r = api_client.post("/api/import/preview", json={
        "kind": "corporate_actions",
        "csv_text": "account,date,kind,from_symbol,to_symbol,ratio_to,ratio_from\n"
                    "tw_broker,2026-06-10,SPLIT,2330,2330,10,1\n"})
    (row,) = r.json()["rows"]
    assert row["data"]["kind_label"] == "分割"


def test_the_pages_print_the_wire_label() -> None:
    detail = (_WEB / "detail.js").read_text(encoding="utf-8")
    assert "t.kind_label || t.kind" in detail and "u.kind_label || u.kind" in detail
    assert "u.kind_label || u.kind" in (_WEB / "app.js").read_text(encoding="utf-8")
    assert "d.kind_label || d.kind" in (_WEB / "input.js").read_text(encoding="utf-8")
