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

**The non-blocking twins** (PERF spec 2026-10-18 §9.3 phase 2): a settlement run lists every
待釐清 record of the dashboard's TOLERANT replay and goes on — a strict refusal 「無法重算」 would
claim the run stopped at the first one. Its findings are worded 「以待釐清處理：…」 and live here,
beside the strict sentence, one owner per sentence: :func:`oversold_pending_message` for a 賣超,
and :func:`pending_finding_text` for every other kind, which prefixes the replay's OWN reason
sentence (the one the strict path raises), so the two paths explain a record in the same words.
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


#: The prefix of every non-blocking finding (the settlement's phase 2, the run report).
PENDING_PREFIX = "以待釐清處理："


def oversold_pending_message(row: OversoldRow) -> str:
    """The non-blocking twin of :func:`oversold_position_message`:
    「以待釐清處理：帳本中有賣超部位（{account:…}／<sym>，<date>）— 該部位的成本基礎已捨棄、
    沒有已實現損益，估值與 XIRR 暫不計算；結算照常完成，請修正該筆交易」."""
    return (f"{PENDING_PREFIX}帳本中有賣超部位（{account_ref(row.account_id)}／{row.symbol}，"
            f"{row.trade_date.isoformat()}）— 該部位的成本基礎已捨棄、沒有已實現損益，"
            "估值與 XIRR 暫不計算；結算照常完成，請修正該筆交易")


def pending_finding_text(reason: str) -> str:
    """A non-blocking finding over the replay's own zh *reason* sentence (an unapplied action,
    a refused dividend or close, an unregistered symbol): 「以待釐清處理：<reason>」. The reason
    keeps its own remedy; the prefix says the run went on."""
    return f"{PENDING_PREFIX}{reason}"


def unregistered_symbol_reason(symbol: str, first_day: date) -> str:
    """Why an unregistered symbol's rows are left out (the dashboard's degradation, 1b):
    「<sym> 尚未登錄標的（最早一筆 <date>），相關的交易、股利與期初庫存不計入；登錄後即會計入」."""
    return (f"{symbol} 尚未登錄標的（最早一筆 {first_day.isoformat()}），"
            "相關的交易、股利與期初庫存不計入；登錄後即會計入")
