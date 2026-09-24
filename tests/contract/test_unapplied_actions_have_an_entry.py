"""DEF-023 (functional test manual D-11, 2026-09-23): an unapplied corporate action has a
visible entry — on the dashboard, on its own ledger row, and from the drawer.

Reproduction: buy 2882 (2026-09-22) → record a 1→2 SPLIT (2026-09-23) → delete the buy.
The replay then refuses the split; XIRR reads 「— 資料不足」 for the WHOLE portfolio (D38),
and the sentence naming the row lived only in that card's hover title. The 公司行動 tab
showed no mark on the row, the trade's delete confirm had not warned, and only the 2882
drawer said 「⚠ 公司行動未套用」.

Four surfaces, checked two ways:

* **the ledger API** carries the replay's refusal ON THE ROW (``unapplied.reason``) — a
  TestClient test over a poisoned ledger;
* **the pages** — the dashboard banner, the row mark, the deep link, the delete warning,
  the drawer link — are static vanilla JS with no build step, so their rendering code is
  asserted statically: the block exists, every account goes through ``pdNames`` (via the
  file's ``acctZh``), and the link targets the ledger row.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

_WEB = Path(__file__).resolve().parents[2] / "web"
_BASE = "/api/ledgers/corporate-actions"


def _poison(conn: sqlite3.Connection, **over: str) -> None:
    """A SPLIT dated BEFORE the golden 2330 buy (2026-01-05): E1, 「沒有持倉，無法套用」.
    Written straight to the table — the entry doors refuse it (E1a), which is the point:
    this is the state a later trade deletion leaves behind."""
    row = {"account_id": "tw_broker", "date": "2025-12-01", "kind": "SPLIT",
           "from_symbol": "2330", "to_symbol": "2330", "ratio_to": "2", "ratio_from": "1"}
    row.update(over)
    conn.execute(
        "INSERT INTO corporate_actions (account_id,date,kind,from_symbol,to_symbol,"
        "ratio_to,ratio_from,cost_carry,note) VALUES (?,?,?,?,?,?,?,NULL,NULL)",
        tuple(row[k] for k in ("account_id", "date", "kind", "from_symbol", "to_symbol",
                               "ratio_to", "ratio_from")))
    conn.commit()


# ------------------------------------------------------------------------ the ledger row


def test_the_ledger_row_carries_the_replays_refusal(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _poison(golden_db)
    (row,) = api_client.get(_BASE).json()["rows"]
    assert row["unapplied"] is not None
    assert "沒有持倉" in row["unapplied"]["reason"]
    # …and the dashboard names the same row, so the banner and the mark agree.
    dash = api_client.get("/api/dashboard").json()["unapplied_actions"]
    assert len(dash) == 1 and dash[0]["date"] == "2025-12-01"


def test_a_row_the_replay_applies_carries_no_mark(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Detection power: the field must be null when nothing is wrong."""
    r = api_client.post(_BASE, json={
        "account_id": "tw_broker", "date": date(2026, 6, 10).isoformat(), "kind": "SPLIT",
        "from_symbol": "2330", "to_symbol": "2330", "ratio_to": "10", "ratio_from": "1"})
    assert r.status_code == 201, r.text
    (row,) = api_client.get(_BASE).json()["rows"]
    assert row["unapplied"] is None


def test_an_unreadable_row_is_marked_too(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The third way a row goes unapplied — a shape the loader cannot convert (a hand
    edit) — is marked with the loader's own zh sentence, not skipped."""
    _poison(golden_db, date="2026-06-10", ratio_to="0.2857")
    (row,) = api_client.get(_BASE).json()["rows"]
    assert row["unapplied"] is not None and "0.2857" in row["unapplied"]["reason"]


def test_the_list_stays_up_when_the_ledger_cannot_be_replayed_at_all(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """A list page that 500s on a broken ledger is a list page that cannot be used to fix
    it. `_unapplied_index` degrades to no marks; the rows still list."""
    _poison(golden_db, kind="MERGER")
    r = api_client.get(_BASE)
    assert r.status_code == 200
    assert len(r.json()["rows"]) == 1


# ----------------------------------------------------------------------------- the pages


def _src(name: str) -> str:
    return (_WEB / name).read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    """The body of `function name(` up to the next top-level `function ` (rough, enough)."""
    start = src.index(f"function {name}(")
    nxt = re.search(r"\n  (?:async )?function ", src[start + 1:])
    return src[start:start + 1 + nxt.start()] if nxt else src[start:]


def test_the_dashboard_renders_a_visible_block_with_an_entry_per_row() -> None:
    src = _src("app.js")
    body = _fn(src, "renderUnappliedBanner")
    assert "D.unapplied_actions" in body
    assert "unapplied-banner" in body and "tooltip" not in body.lower()
    # Every account name through the single naming authority (web/names.js).
    assert "acctZh(u.account_id)" in body
    assert "u.account" not in body.replace("u.account_id", "")
    # Structured fields, the reason verbatim, and a link to the ledger row.
    for field in ("u.date", "u.kind", "u.reason", "u.to_symbol"):
        assert field in body, field
    assert "unappliedActionHref(u)" in body
    href = _fn(src, "unappliedActionHref")
    assert "trades.html?ledger=action" in href and "action_id" in href
    # Wired into the render pass (not defined and forgotten). ⚠ R2 bounce: this used to assert
    # the call sat inside renderUnregisteredBanner — which it did, AFTER that function's early
    # return, so the block never rendered without an unregistered symbol. Whether it RENDERS
    # is now proven by running the code: tests/contract/test_def023_unapplied_banner_renders.py.
    # Here only the negative is pinned: it is not nested under the other banner again.
    assert "renderUnappliedBanner();" not in _fn(src, "renderUnregisteredBanner")
    assert "renderUnappliedBanner();" in _fn(src, "renderHeader")
    assert "renderUnregisteredBanner();" in _fn(src, "renderHeader")
    # And it renders NOTHING on a clean ledger.
    assert "if (!rows.length || !page) return;" in body


def test_the_ledger_marks_the_row_and_answers_the_deep_link() -> None:
    src = _src("ledger.js")
    render = _fn(src, "renderActions")
    assert "tr.dataset.actionId = a.id" in render
    assert "a.unapplied" in render and "'未套用'" in render
    assert "a.unapplied.reason" in render
    link = _fn(src, "openDeepLink")
    assert "'ledger'" in link and "'action_id'" in link
    assert "tr[data-action-id=" in link and "ledger-added-row" in link
    assert "boot().then(openDeepLink)" in src


def test_the_trade_delete_confirm_warns_about_a_later_action() -> None:
    src = _src("ledger.js")
    warn = _fn(src, "delTxWithWarning")
    assert "/api/ledgers/corporate-actions" in warn
    assert "a.date >= t.date" in warn and "a.symbol === t.symbol" in warn
    assert "可能失去持倉依據而無法套用" in warn
    assert "delTxWithWarning(t)" in _fn(src, "renderTx")


def test_the_drawer_uses_structured_fields_and_links_to_the_row() -> None:
    src = _src("detail.js")
    body = _fn(src, "renderActionIssues")
    assert "acctZh(u.account_id)" in body
    assert "u.account" not in body.replace("u.account_id", "")
    assert "trades.html?ledger=action" in body and "前往公司行動帳本" in body
    # The kind in the ledger's own vocabulary — the wire's `kind_label` (I-14: the private
    # copy of shared/corporate_actions.py::KIND_ZH that used to live here is gone).
    assert "u.kind_label || u.kind" in body and "SPLIT:" not in body


def test_the_dashboard_banner_has_its_styles() -> None:
    css = _src("styles.css")
    for cls in (".unapplied-banner", ".unapplied-list", ".unapplied-item", ".ledger-unapplied"):
        assert cls in css, cls
