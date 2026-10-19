"""The ledger check of a settlement run (PERF spec §9.3 phase 2; plan §6.1 "ledger_findings").

The tolerant replay lists EVERY record it keeps as 待釐清 — never only the first, as the strict
重算 does — each dated, in non-blocking wording (「以待釐清處理：…」, ``shared/oversold.py``),
quoting the replay's own reason sentence; unregistered symbols are listed and excluded exactly
as the dashboard excludes them; the events processed are counted.
"""

from __future__ import annotations

from datetime import date

from portfolio_dash.data_ingestion.store import load_ledger_bundle
from portfolio_dash.portfolio.cost_basis import UnbookableLedgerError, build_book
from portfolio_dash.portfolio.ledger_findings import ledger_findings, review_ledger
from portfolio_dash.shared.oversold import (
    PENDING_PREFIX,
    oversold_pending_message,
    oversold_position_message,
    pending_finding_text,
)
from tests.reference.corpora import open_db
from tests.settlement.conftest import CorpusFactory


def test_every_refusal_of_the_tolerant_replay_is_listed_and_dated(
        corpus_db: CorpusFactory) -> None:
    db = corpus_db("rich_refusals")
    conn = open_db(db.path)
    try:
        bundle = load_ledger_bundle(conn)
    finally:
        conn.close()
    cut = bundle.valued_as_of(db.now.date())
    review = review_ledger(cut)
    kinds = [f.kind for f in review.findings]
    assert sorted(set(kinds)) == ["orphan_dividend", "oversold", "refused_close",
                                  "unapplied_action", "unbookable_dividend"]
    assert kinds.count("unapplied_action") == 2  # the unreadable row AND the refused one
    assert all(f.day is not None and f.account_id and f.symbol for f in review.findings)
    assert all(f.text.startswith(PENDING_PREFIX) for f in review.findings)
    book = review.book
    reasons = ([a.reason for a in book.unapplied_actions]
               + [c.reason for c in book.refused_closes]
               + [u.reason for u in book.unbookable_dividends])
    for reason in reasons:  # the replay's own sentence, quoted after the prefix
        assert any(f.text == pending_finding_text(reason) for f in review.findings), reason
    assert review.events == (len(cut.transactions) + len(cut.dividends) + len(cut.opening)
                             + len(cut.actions) + len(cut.unreadable_actions))
    # the strict replay stops at its first refusal; the check never does
    try:
        build_book(cut)
    except UnbookableLedgerError:
        pass
    else:  # pragma: no cover - the corpus is built to refuse
        raise AssertionError("rich_refusals replays strictly")


def test_a_clean_ledger_has_no_findings(corpus_db: CorpusFactory) -> None:
    db = corpus_db("rich")
    conn = open_db(db.path)
    try:
        assert ledger_findings(load_ledger_bundle(conn)) == []
    finally:
        conn.close()


def test_an_unregistered_symbol_is_listed_and_excluded_as_the_dashboard_excludes_it(
        corpus_db: CorpusFactory) -> None:
    db = corpus_db("golden")
    conn = open_db(db.path)
    try:
        conn.execute("INSERT INTO transactions (account_id, symbol, side, quantity, price, "
                     "fees, tax, trade_date) VALUES ('tw_broker', 'NOSUCH', 'BUY', '10', '5', "
                     "'0', '0', '2026-02-11')")
        conn.execute("INSERT INTO transactions (account_id, symbol, side, quantity, price, "
                     "fees, tax, trade_date) VALUES ('tw_broker', 'NOSUCH', 'BUY', '10', '5', "
                     "'0', '0', '2026-02-09')")
        conn.commit()
        bundle = load_ledger_bundle(conn)
    finally:
        conn.close()
    review = review_ledger(bundle)
    [finding] = review.findings
    assert finding.kind == "unregistered_symbol" and finding.symbol == "NOSUCH"
    assert finding.day == date(2026, 2, 9) and finding.account_id == "tw_broker"
    assert "尚未登錄標的" in finding.text and finding.text.startswith(PENDING_PREFIX)
    assert review.unregistered == ("NOSUCH",)
    assert all(h.symbol != "NOSUCH" for h in review.book.holdings)


def test_the_oversold_twin_sits_beside_the_strict_sentence() -> None:
    class Row:
        account_id = "tw_broker"
        symbol = "2330"
        trade_date = date(2026, 1, 12)

    strict = oversold_position_message(Row())
    twin = oversold_pending_message(Row())
    assert strict.startswith("帳本中有賣超部位待釐清（{account:tw_broker}／2330，2026-01-12）")
    assert twin.startswith(PENDING_PREFIX + "帳本中有賣超部位（{account:tw_broker}／2330，"
                           "2026-01-12）")
    assert "結算照常完成" in twin and "無法" not in twin
