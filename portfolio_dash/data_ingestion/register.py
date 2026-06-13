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

from portfolio_dash.data_ingestion.store import upsert_instrument
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
                message=f"could not resolve TW board for {instrument.symbol!r}; set it manually",
            )
        )

    inst = instrument.model_copy(update={"board": board})
    written = False
    hard = [i for i in issues if not i.needs_confirm]
    if confirm and not hard:
        upsert_instrument(conn, inst)
        status = "unresolved" if (inst.market is Market.TW and not board) else "resolved"
        conn.execute("UPDATE instruments SET board_status=? WHERE symbol=?",
                     (status, inst.symbol))
        conn.commit()
        written = True
    return InstrumentDraft(instrument=inst, issues=issues, written=written)
