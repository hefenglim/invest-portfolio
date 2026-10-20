"""X1 (owner ruling 2026-10-05): every surface that LISTS dividend rows marks a refused one.

「標示『未入帳（待釐清）』並排除合計」 — mark AND exclude from totals. ⑪ took a payout the replay
refuses (no position by its effective date, an open declared short, a ticker an EXCHANGE
vacated) out of every cash figure; the 現金收支明細 statement already listed it marked. The
ledger's own listings did not: the 交易帳本 股利 tab, the printed 帳本報告 (whose 淨額合計 still
summed it), the drawer's 配息史 and its reconciliation CSV, and the AI-facing dividend rows —
which handed the model a refused payout as a plain dividend.

One decision, never re-derived: ``portfolio.returns.DividendRefusals`` — the replay's refused
rows with the replay's own sentence for each (CR-02's orphan sentence, K3's short / vacated
sentence). The reason on every surface is THAT sentence, so the ledger tab, the drawer and the
statement explain one row in the same words. Each surface family is exercised for all three
refusal classes, against the same ledger without the refused row (the control).

The raw per-tab ledger CSV (``/api/export/ledger``) is deliberately NOT marked: it is a
``SELECT *`` dump that re-imports (DEF-026), the same reason DEF-056 left its ``counts_from``
off — pinned below so the decision is visible.
"""

from __future__ import annotations

import csv
import html
import io
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.portfolio.cash import UNBOOKED_DIVIDEND_REASON
from tests.conftest import DashboardClientFactory
from tests.contract.test_ruling11_refused_dividends_move_no_cash import (
    _CLASSES,
    _NOTE,
    _REFUSED_NET,
    _pair,
)

#: class → the cause the replay records, in its own vocabulary (CR-02 / K3).
_CAUSE = {"orphan": "orphan", "open_short": "short", "vacated": "vacated"}


@pytest.fixture(params=sorted(_CLASSES))
def cls(request: pytest.FixtureRequest) -> str:
    name: str = request.param
    return name


def _json(r: Any) -> Any:
    assert r.status_code == 200, r.text
    return r.json()


def _refused(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("booked") is False]


def _assert_reason(reason: Any, symbol: str) -> None:
    """The replay's OWN sentence for this row — not empty, not the generic fallback, and about
    this symbol (the cost_basis sentences name it; their wording is theirs to change)."""
    assert isinstance(reason, str) and reason, reason
    assert reason != UNBOOKED_DIVIDEND_REASON, reason
    assert symbol in reason, reason


# --- the 交易帳本 股利 tab ------------------------------------------------------------------


def test_the_ledger_tab_marks_the_refused_row_with_the_replays_reason(
    dashboard_client_factory: DashboardClientFactory, cls: str,
) -> None:
    refused, control = _pair(dashboard_client_factory, cls)
    symbol = _CLASSES[cls][1]
    got = _json(refused.get("/api/ledgers/dividends"))
    base = _json(control.get("/api/ledgers/dividends"))
    marked = _refused(got["rows"])
    assert len(marked) == 1, (cls, got["rows"])
    (row,) = marked
    assert (row["symbol"], row["net"]) == (symbol, _REFUSED_NET)
    assert row["unbooked_note"] == _NOTE
    _assert_reason(row["unbooked_reason"], symbol)
    # Listed — it is a ledger row the owner must be able to find and correct …
    assert got["total_count"] == base["total_count"] + 1
    # … and every other row reads as before, booked.
    assert all(r["booked"] is True and r["unbooked_note"] is None
               and r["unbooked_reason"] is None for r in base["rows"])
    keep = ("id", "date", "symbol", "type", "gross", "net", "counts_from")
    assert ([{k: r[k] for k in keep} for r in got["rows"] if r["booked"]]
            == [{k: r[k] for k in keep} for r in base["rows"]])
    # The tab's filters mark it the same way.
    only = _json(refused.get("/api/ledgers/dividends", params={"symbol": symbol}))["rows"]
    assert [r["booked"] for r in only] == [False]
    acct = _json(refused.get("/api/ledgers/dividends", params={"account_id": "tw_broker"}))
    assert len(_refused(acct["rows"])) == 1


def test_the_statement_explains_the_row_in_the_same_words(
    dashboard_client_factory: DashboardClientFactory, cls: str,
) -> None:
    """One decision, one sentence: the 現金收支明細 row (⑪) carried a generic reason; it now
    carries the replay's sentence for THAT row — the ledger tab's, word for word."""
    refused, _control = _pair(dashboard_client_factory, cls)
    ledger = _refused(_json(refused.get("/api/ledgers/dividends"))["rows"])[0]
    stmt = _json(refused.get("/api/cash/statement",
                             params={"account": "tw_broker", "ccy": "TWD", "limit": 500}))
    (row,) = [r for r in stmt["rows"] if r["booked"] is False]
    assert row["unbooked_reason"] == ledger["unbooked_reason"], (row, ledger)


# --- the printed 帳本報告 and 現金收支明細 ------------------------------------------------------


_NET_TOTAL = re.compile(r"淨額合計：(?P<totals>[^<]*)</p>")


def _ledger_report(client: TestClient) -> str:
    r = client.post("/api/export/ledgers-report", json={})
    assert r.status_code == 200, r.text
    return str(r.content.decode("utf-8"))


def test_the_printed_ledger_report_marks_it_and_leaves_it_out_of_the_total(
    dashboard_client_factory: DashboardClientFactory, cls: str,
) -> None:
    refused, control = _pair(dashboard_client_factory, cls)
    symbol = _CLASSES[cls][1]
    got, base = _ledger_report(refused), _ledger_report(control)
    g, b = _NET_TOTAL.search(got), _NET_TOTAL.search(base)
    assert g is not None and b is not None
    # 淨額合計 is the control's: the 300 is listed, not summed.
    assert g.group("totals") == b.group("totals"), (cls, g.group("totals"), b.group("totals"))
    assert '<tr class="unbooked">' in got and f'<span class="chip unbooked">{_NOTE}</span>' in got
    assert "其中 1 筆標示「未入帳（待釐清）」，不計入淨額合計" in got
    reason = _refused(_json(refused.get("/api/ledgers/dividends"))["rows"])[0][
        "unbooked_reason"]
    assert f"{symbol}・{_NOTE}：{html.escape(reason)}</p>" in got   # the row's own sentence
    assert _NOTE not in base and "unbooked" not in base


def test_the_printed_statement_gives_each_refused_row_its_reason(
    dashboard_client_factory: DashboardClientFactory, cls: str,
) -> None:
    refused, _control = _pair(dashboard_client_factory, cls)
    symbol = _CLASSES[cls][1]
    r = refused.post("/api/export/cash-statement-report",
                     json={"account": "tw_broker", "ccy": "TWD"})
    assert r.status_code == 200, r.text
    doc = r.content.decode("utf-8")
    reason = _refused(_json(refused.get("/api/ledgers/dividends"))["rows"])[0][
        "unbooked_reason"]
    assert f"・{symbol}・{_NOTE}：{html.escape(reason)}</p>" in doc, doc[-2000:]
    assert UNBOOKED_DIVIDEND_REASON not in doc   # the row's own sentence, not the generic one


# --- the drawer's 配息史 and its reconciliation CSV ------------------------------------------


def test_the_drawer_lists_the_refused_row_marked(
    dashboard_client_factory: DashboardClientFactory, cls: str,
) -> None:
    refused, control = _pair(dashboard_client_factory, cls)
    symbol = _CLASSES[cls][1]
    got = _json(refused.get(f"/api/symbol/{symbol}/detail"))["dividend_events"]
    (row,) = _refused(got)
    assert row["net"] == _REFUSED_NET and row["unbooked_note"] == _NOTE
    _assert_reason(row["unbooked_reason"], symbol)
    booked = _json(control.get("/api/symbol/2330/detail"))["dividend_events"]
    assert booked and all(r["booked"] is True and r["unbooked_note"] is None for r in booked)
    assert all(r["booked"] is True
               for r in _json(refused.get("/api/symbol/2330/detail"))["dividend_events"])


def _symbol_csv(client: TestClient, symbol: str) -> list[list[str]]:
    r = client.post("/api/export/symbol-detail", json={"symbol": symbol})
    assert r.status_code == 200, r.text
    return [row for row in csv.reader(io.StringIO(r.content.decode("utf-8-sig")))
            if row and not row[0].startswith("#")]


def test_the_drawers_reconciliation_csv_marks_the_refused_row(
    dashboard_client_factory: DashboardClientFactory, cls: str,
) -> None:
    refused, _control = _pair(dashboard_client_factory, cls)
    symbol = _CLASSES[cls][1]
    table = _symbol_csv(refused, symbol)
    head = table[0]
    assert head[-2:] == ["unbooked", "counts_from"], head
    col = head.index("unbooked")
    assert [r[col] for r in table[1:]] == [_NOTE], table
    assert all(r[col] == "" for r in _symbol_csv(refused, "2330")[1:])


def test_the_raw_ledger_csv_stays_a_raw_dump(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """DEF-026: the per-tab ledger CSV is ``SELECT *`` and re-imports — a computed column would
    break the round trip, exactly why DEF-056 left ``counts_from`` off it. Not marked, by
    design; the tab and the printed report carry the mark."""
    refused, _control = _pair(dashboard_client_factory, "orphan")
    r = refused.post("/api/export/ledger", json={"kind": "dividends"})
    assert r.status_code == 200, r.text
    head = next(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert "unbooked" not in head and "booked" not in head


# --- the AI-facing dividend rows ------------------------------------------------------------


def _ai_rows(client: TestClient) -> list[dict[str, Any]]:
    import json

    r = client.post("/api/prompts/preview",
                    json={"body": "{{dividends_json}}", "scope": "portfolio", "symbol": None})
    rendered = _json(r)["rendered"]
    rows: list[dict[str, Any]] = json.loads(rendered)
    return rows


def test_the_ai_never_reads_a_refused_payout_as_income(
    dashboard_client_factory: DashboardClientFactory, cls: str,
) -> None:
    refused, control = _pair(dashboard_client_factory, cls)
    symbol = _CLASSES[cls][1]
    got, base = _ai_rows(refused), _ai_rows(control)
    marked = [r for r in got if r.get("booked") is False]
    assert len(marked) == 1, (cls, got)
    (row,) = marked
    assert row["symbol"] == symbol
    assert row["unbooked"] == _NOTE and row["unbooked_cause"] == _CAUSE[cls]
    # A booked row carries no extra key: a ledger with no refusal feeds the model the same
    # JSON as before, so no prompt fingerprint moves.
    assert all(set(r) == {"symbol", "date", "type", "gross", "withholding", "net",
                          "reinvest_shares", "ccy"} for r in base)
    assert [r for r in got if "booked" not in r] == base


# --- structure: every reader of the stored dividend rows is a reviewed one ----------------

#: Who reads the stored dividend ledger, and what it does with a refused row. A NEW reader
#: fails here until it is reviewed: a listing must mark the row (X1), a figure must leave it
#: out (⑪), and neither may re-derive the refusal.
_DIVIDEND_READERS = {
    # listings — marked 「未入帳（待釐清）」 with the replay's reason, out of every total (X1)
    "api/routers/ledgers.py::dividends",
    "api/routers/symbol.py::symbol_detail",
    "api/routers/prompts.py::_dividend_rows",
    "export/ledgers_report.py::build_ledgers_report_html",
    "export/symbol_detail.py::build_symbol_detail_csv",
    # cash — split into booked / unbooked by DividendRefusals (⑪); the statement lists both
    "api/routers/cash.py::load_cash_ledgers",
    "export/cash_statement.py::_load",
    "portfolio/dashboard.py::_build_dashboard",
    # replay inputs — the rows the refusal is decided FROM
    "api/replay_guard.py::_read_ledgers",
    "data_ingestion/store.py::load_ledger_bundle",
    # the settlement's one read snapshot: the replay's bundle, and its cash series through
    # booked_dividends(…, the replay's refused_dividends) — the dashboard's split (⑪)
    "portfolio/settle_worker.py::_load",
    # the inbox's "already recorded?" guard: a refused row IS recorded — offering the event
    # again would invite a duplicate of a row the owner has to correct, not add to
    "api/dividend_inbox.py::detect",
}


def test_every_reader_of_the_dividend_ledger_is_reviewed() -> None:
    from tests.contract.test_ruling11_refused_dividends_move_no_cash import _callers_of

    assert _callers_of("list_dividends") == _DIVIDEND_READERS
