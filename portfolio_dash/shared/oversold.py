"""The ONE sentence for an unresolved 賣超 position (DEF-008, 2026-09-23).

``portfolio.cost_basis.OversellError`` carries the offending row as fields (account, symbol,
trade date) so no caller has to regex a sentence for them. Four strict callers then turned
those fields into the SAME zh sentence by hand — the app's 422 handler, 重算, the tax package
and the drawer 試算 — each embedding the bare ``account_id`` (「帳本中有賣超部位待釐清（tw_broker
／2330，…」), the one spelling of an account no other surface uses. Four copies is how a
wording fix reaches three doors and misses the fourth, so the sentence and its structured
issue live here, once, and name the account by TOKEN (``shared/account_ref.py``): the fetch
layer resolves it to the ``pdNames`` spelling.

L0 (``shared/``): it takes the row structurally (:class:`OversoldRow`), so ``strategy/`` and
``api/`` can both call it without ``shared/`` importing ``portfolio/``.
"""

from datetime import date
from typing import Protocol

from portfolio_dash.shared.account_ref import account_ref


class OversoldRow(Protocol):
    """What the sentence needs off the error: the row that could not be replayed."""

    account_id: str
    symbol: str
    trade_date: date


def oversold_position_message(row: OversoldRow, consequence: str | None = None) -> str:
    """「帳本中有賣超部位待釐清（{account:…}／<sym>，<date>）— [<consequence>，]請先修正該筆交易」.

    *consequence* is what this door could not do (「無法重算」「無法試算」…); ``None`` for the
    app-wide handler, which has no single door to name.
    """
    tail = f"{consequence}，請先修正該筆交易" if consequence else "請先修正該筆交易"
    return (f"帳本中有賣超部位待釐清（{account_ref(row.account_id)}／{row.symbol}，"
            f"{row.trade_date.isoformat()}）— {tail}")


def oversold_position_issues(row: OversoldRow, detail: str) -> list[dict[str, object]]:
    """The structured twin of the sentence: the row as FIELDS, *detail* as the English text.

    ``detail`` is ``str(exc)`` — written for a developer by design (「sell 100 > held 50 for
    2330」), so it rides in ``issues[].text`` and never in the rendered ``message``.
    """
    return [{
        "sev": "error",
        "code": "oversold_position",
        "text": detail,
        "field": None,
        "account_id": row.account_id,
        "symbol": row.symbol,
        "trade_date": row.trade_date.isoformat(),
    }]
