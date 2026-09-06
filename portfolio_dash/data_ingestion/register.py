"""Instrument registration: resolve board, then persist on confirm.

`register_instrument` fills the instrument's board (US/MY deterministic; TW via an
**injected** prober, so this module stays decoupled from `pricing`) and upserts it
when confirmed. An unresolved TW board is a soft ``board_unresolved`` flag that does
not block registration (the work-list's TWSE fallback keeps quotes working until the
user sets it). The listing/confirm UI is `web_ui/`.
"""

import sqlite3
from collections.abc import Callable

from pydantic import BaseModel, Field

from portfolio_dash.data_ingestion.store import get_instrument, upsert_instrument
from portfolio_dash.data_ingestion.validate import Issue
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.models.assets import Instrument

BoardProber = Callable[[str], str | None]

_MARKET_DEFAULT_BOARD: dict[Market, str] = {Market.US: "", Market.MY: ".KL"}


class InstrumentDraft(BaseModel):
    """Outcome of a registration attempt (preview when not confirmed, else written)."""

    instrument: Instrument
    issues: list[Issue] = Field(default_factory=list)
    written: bool = False


def register_instrument(
    conn: sqlite3.Connection,
    instrument: Instrument,
    *,
    prober: BoardProber | None = None,
    confirm: bool = False,
    commit: bool = True,
) -> InstrumentDraft:
    """Resolve the instrument's board and (on confirm) persist it.

    A non-empty ``instrument.board`` is respected as-is (a user-confirmed/edited value;
    the prober is not called). Otherwise: US/MY get their deterministic board; TW is
    probed via *prober* if given. A TW instrument left without a board gets a soft
    ``board_unresolved`` issue but still writes on confirm.
    """
    board = instrument.board
    if not board:
        if instrument.market in _MARKET_DEFAULT_BOARD:
            board = _MARKET_DEFAULT_BOARD[instrument.market]
        elif instrument.market is Market.TW and prober is not None:
            board = prober(instrument.symbol) or ""

    issues: list[Issue] = []
    if instrument.market is Market.TW and not board:
        issues.append(
            Issue(
                kind="board_unresolved",
                needs_confirm=True,
                message=f"無法判定 {instrument.symbol} 的上市／上櫃別 — 請手動指定",
            )
        )

    inst = instrument.model_copy(update={"board": board})
    written = False
    hard = [i for i in issues if not i.needs_confirm]
    if confirm and not hard:
        upsert_instrument(conn, inst, commit=False)
        status = "unresolved" if (inst.market is Market.TW and not board) else "resolved"
        conn.execute("UPDATE instruments SET board_status=? WHERE symbol=?",
                     (status, inst.symbol))
        # The two statements above were always ONE logical write, and the single commit is
        # what makes them atomic together. ``commit=False`` hands that job to an enclosing
        # all-or-nothing batch (D48a's auto-register runs inside one).
        if commit:
            conn.commit()
        written = True
    return InstrumentDraft(instrument=inst, issues=issues, written=written)


def autoregister_spinoff_child(
    conn: sqlite3.Connection, *, parent_symbol: str, child_symbol: str,
    commit: bool = True,
) -> Instrument | None:
    """D48a: create the instrument a SPINOFF is about to hand shares to.

    Owner ruling 2026-08-15 — 「spinoff則是分割出來的股份為自動註冊新股ticker」. Before this,
    E10 hard-rejected an unregistered destination, which asked the owner to pre-create a row
    for a security that does not exist until the very event they are recording.

    **Market and quote currency are INHERITED from the parent, not guessed.** E11 already
    requires both ends of an action to share a quote currency, so inheriting is the only
    value that could pass validation anyway — it is a derivation, not an assumption. Name,
    sector and industry are left blank rather than copied: they are facts about a different
    company, and a child carrying its parent's name is worse than one carrying none. **So is
    the ETF flag** (NEW-21, 2026-09-06): it is left UNANSWERED (``etf_flag_unknown=True``), not
    copied and not defaulted — the model default ``False/False`` reads as "answered: not an
    ETF", which silenced AI-D40's ``etf_flag_unknown`` disclosure on a spun-off TW ETF's first
    sell. Same posture as ``quick_register`` handed ``is_etf=None``.

    **No network call.** Registration must not be able to fail because a provider has not
    listed the child yet — which, for a spin-off, is the normal case on day one. The price
    arrives later, from the scheduled refresh or from the form's own 起始價 field (D48b).

    Returns the created instrument, or ``None`` when there was nothing to do (the child
    already exists, or the parent does not — the latter is E10's hard rejection, unchanged).
    """
    if get_instrument(conn, child_symbol) is not None:
        return None
    parent = get_instrument(conn, parent_symbol)
    if parent is None:
        return None
    register_instrument(conn, spinoff_child_draft(parent, child_symbol),
                        prober=None, confirm=True, commit=commit)
    return get_instrument(conn, child_symbol)


def spinoff_child_draft(parent: Instrument, child_symbol: str) -> Instrument:
    """The instrument a SPINOFF's child WOULD be — pure, no I/O, no write.

    One statement of the inheritance rule, used twice: :func:`autoregister_spinoff_child`
    persists it, and the form's preview drops it into an in-memory bundle so the
    before/after replay can value a position in a symbol that does not exist yet. Two copies
    would let the preview show a conservation check computed under one quote currency while
    the save wrote another — §5.1's two-numbers-on-one-screen failure, arriving as a
    ✓ 成本不變 over a book built from different assumptions.
    """
    return Instrument(symbol=child_symbol, market=parent.market,
                      quote_ccy=parent.quote_ccy, sector="", name="",
                      # NEW-21: nobody has answered whether the CHILD is an ETF. Not inherited
                      # (a fact about another company) and not the model's False default
                      # (which means "answered: no") — the fee engine computes with False and
                      # DISCLOSES it on a TW SELL, exactly as for a manual auto-registration.
                      etf_flag_unknown=True)
