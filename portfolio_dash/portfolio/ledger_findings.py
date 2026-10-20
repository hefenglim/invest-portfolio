"""The ledger check of a settlement run (PERF spec 2026-10-18 §9.3 phase 2 「檢查帳本」).

A pure ``portfolio/`` function over the bundle the run settles. It runs the dashboard's
TOLERANT replay (``build_book(..., allow_oversell=True)``) — the replay every page values the
ledger with — and lists **every** record that replay keeps as 待釐清, each dated, in
non-blocking wording (``shared/oversold.py``'s 「以待釐清處理：…」 twins):

- every 賣超 position (``Book.oversold_positions``);
- every dividend refused for having no position (``orphan_dividends``) or because it landed on
  an open declared short or a vacated ticker (``unbookable_dividends``);
- every corporate action the replay could not apply (``unapplied_actions``), the unreadable
  stored rows included;
- every option close row it refused (``refused_closes``);
- every unregistered symbol, whose rows are excluded exactly as the dashboard excludes them
  (``LedgerBundle.without_unregistered``).

Findings never stop a run. The strict 重算 replay stops at its first refusal and says 「無法重算」;
that would list one item and claim the run stopped (run-report review #5). The worker may not
call the 重算 route either (it imports ``fastapi``), hence this function.

Imports nothing above ``portfolio/`` and ``shared/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from portfolio_dash.portfolio.cost_basis import build_book, orphan_dividend_message
from portfolio_dash.portfolio.results import Book
from portfolio_dash.portfolio.settlement_models import Finding
from portfolio_dash.shared.models.ledger import LedgerBundle
from portfolio_dash.shared.oversold import (
    oversold_pending_message,
    pending_finding_text,
    unregistered_symbol_reason,
)


@dataclass
class _OversoldRow:
    """``shared.oversold.OversoldRow`` off a book's ``OversoldPosition``."""

    account_id: str
    symbol: str
    trade_date: date


@dataclass(frozen=True)
class LedgerReview:
    """What the check found: the tolerant replay's *book* of the registered ledger (the run's
    refusals at its last day come from it), the findings, and the events it processed."""

    book: Book
    findings: list[Finding]
    #: Every event the tolerant replay processed: trades, dividends, openings, corporate
    #: actions and unreadable action rows of the registered ledger (a refusal is recorded,
    #: never a stop, so every event is processed).
    events: int
    #: The unregistered symbols, sorted (their rows are not in *book*).
    unregistered: tuple[str, ...]


def _first_days(bundle: LedgerBundle, symbols: set[str]) -> dict[str, tuple[date, set[str]]]:
    """For each symbol, its earliest ledger day and the accounts its rows are in."""
    out: dict[str, tuple[date, set[str]]] = {}

    def see(symbol: str, day: date, account_id: str) -> None:
        if symbol not in symbols:
            return
        first, accounts = out.get(symbol, (day, set()))
        accounts.add(account_id)
        out[symbol] = (min(first, day), accounts)

    for t in bundle.transactions:
        see(t.symbol, t.trade_date, t.account_id)
    for d in bundle.dividends:
        see(d.symbol, d.effective_date, d.account_id)
    for o in bundle.opening:
        see(o.symbol, o.build_date, o.account_id)
    for a in bundle.actions:
        see(a.from_symbol, a.date, a.account_id)
        see(a.to_symbol, a.date, a.account_id)
    return out


def review_ledger(bundle: LedgerBundle) -> LedgerReview:
    """Check *bundle* — the ledger the run settles, unregistered rows INCLUDED (they are listed,
    then dropped as the dashboard drops them) — with the tolerant replay.

    Raises :class:`~portfolio_dash.portfolio.cost_basis.UnbookableLedgerError` only where the
    dashboard itself answers 422 (a decimal fault in a row): there is no book to value then.
    """
    unregistered = tuple(bundle.unregistered_symbols)
    registered = bundle.without_unregistered() if unregistered else bundle
    book = build_book(registered, allow_oversell=True)
    findings: list[Finding] = []
    for p in book.oversold_positions:
        day = p.oversold_on
        text = (oversold_pending_message(_OversoldRow(p.account_id, p.symbol, day))
                if day is not None else
                pending_finding_text(f"帳本中有賣超部位（{p.symbol}）"))
        findings.append(Finding(kind="oversold", day=day, account_id=p.account_id,
                                symbol=p.symbol, text=text))
    for d in book.orphan_dividends:
        findings.append(Finding(kind="orphan_dividend", day=d.effective_date,
                                account_id=d.account_id, symbol=d.symbol,
                                text=pending_finding_text(orphan_dividend_message(d))))
    for u in book.unbookable_dividends:
        findings.append(Finding(kind="unbookable_dividend", day=u.dividend.date,
                                account_id=u.dividend.account_id, symbol=u.dividend.symbol,
                                text=pending_finding_text(u.reason)))
    for a in book.unapplied_actions:
        findings.append(Finding(kind="unapplied_action", day=a.date, account_id=a.account_id,
                                symbol=a.from_symbol, text=pending_finding_text(a.reason)))
    for c in book.refused_closes:
        findings.append(Finding(kind="refused_close", day=c.date, account_id=c.account_id,
                                symbol=c.symbol, text=pending_finding_text(c.reason)))
    first = _first_days(bundle, set(unregistered))
    for symbol in unregistered:
        day, accounts = first[symbol]
        findings.append(Finding(
            kind="unregistered_symbol", day=day,
            account_id=next(iter(accounts)) if len(accounts) == 1 else None, symbol=symbol,
            text=pending_finding_text(unregistered_symbol_reason(symbol, day))))
    events = (len(registered.transactions) + len(registered.dividends)
              + len(registered.opening) + len(registered.actions)
              + len(registered.unreadable_actions))
    return LedgerReview(book=book, findings=findings, events=events, unregistered=unregistered)


def ledger_findings(bundle: LedgerBundle) -> list[Finding]:
    """Every 待釐清 record of the tolerant replay of *bundle*, each dated
    (:func:`review_ledger`)."""
    return review_ledger(bundle).findings


__all__ = ["LedgerReview", "ledger_findings", "review_ledger"]
