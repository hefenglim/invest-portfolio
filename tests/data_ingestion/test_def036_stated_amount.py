"""DEF-036 (functional test manual F-05, 2026-09-23): a contradicted amount is flagged.

The measured case: 「2026/09/22 台灣券商 買進 玉山金 2884 100股 成交價 46 成交金額 50,000 元」
parsed to 100 × 46 with status ok and 「成交金額 50,000 元」 parked in ``note`` — the draft
table said 「✓ 解析完整」 over a row whose own source text disagreed with it by 10.9×. The
transaction draft had nowhere to put a stated total, so the door could not compare it.

The fix is a TRANSCRIBED field, ``TxnDraft.stated_amount``: the model copies the statement's
own 成交金額 (it never computes one), and the door — in Decimal, with the engine's own
fee/tax — compares it against ``shares × price``. A contradiction becomes a needs-confirm
``amount_mismatch`` finding naming both figures; the door never picks a winner and never
fills a field from the other. A consistent or absent amount changes nothing.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.agents import (
    _TXN_CSV_COLUMNS,
    STATED_AMOUNT_TOLERANCE,
    AiDraftList,
    Completer,
    TxnDraft,
    ai_agents_input,
)
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.preview import PreviewRow
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.data_ingestion.validate import CashPool
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side


def _pool(account_id: str, ccy: Currency, **kw: object) -> CashPool:
    return CashPool(balance=Decimal("999999999"), low=Decimal("999999999"))


def _setup(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2884", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Financials",
                                       name="玉山金"))


def _completer(*drafts: TxnDraft) -> Completer:
    def _c(prompt: str, schema: type, *, agent: str, conn: object = None,
           images: list[bytes] | None = None, model_override: str | None = None) -> AiDraftList:
        return AiDraftList(rows=list(drafts))
    return _c


def _draft(shares: str, price: str, stated: str | None, side: Side = Side.BUY) -> TxnDraft:
    return TxnDraft(account_id="tw_broker", symbol="2884", side=side,
                    date=date(2026, 9, 22), shares=Decimal(shares), price=Decimal(price),
                    stated_amount=None if stated is None else Decimal(stated))


def _row(conn: sqlite3.Connection, d: TxnDraft) -> PreviewRow:
    res = ai_agents_input(conn, "x", pool=_pool, completer=_completer(d),
                          today=date(2026, 9, 23))
    return res.previews["transactions"].rows[0]


def test_the_measured_contradiction_is_flagged_not_ok(conn: sqlite3.Connection) -> None:
    """The verifier's exact text: 100 股 × 46 = 4,600 against a stated 50,000."""
    _setup(conn)
    row = _row(conn, _draft("100", "46", "50000"))
    found = [i for i in row.issues if i.kind == "amount_mismatch"]
    assert len(found) == 1, row.issues
    issue = found[0]
    # needs-confirm (a warning the owner must look at), never a silent ok and never a guess
    assert issue.needs_confirm is True and issue.info is False
    assert not row.has_hard_issue
    assert "50,000" in issue.message and "4,600" in issue.message
    assert "100" in issue.message and "46" in issue.message
    # the payload carries the flag the draft table keys the un-ticked default on
    assert row.payload["amount_mismatch"] == "1"
    assert row.payload["stated_amount"] == "50000"
    # the door did NOT fill anything from the stated amount
    assert row.payload["quantity"] == "100" and row.payload["price"] == "46"


def test_a_consistent_amount_is_silent(conn: sqlite3.Connection) -> None:
    _setup(conn)
    row = _row(conn, _draft("100", "46.45", "4645"))
    assert not [i for i in row.issues if i.kind == "amount_mismatch"]
    assert "amount_mismatch" not in row.payload
    assert row.payload["stated_amount"] == "4645"


def test_a_fee_inclusive_total_is_consistent_too(conn: sqlite3.Connection) -> None:
    """應付金額 = 成交金額 + 手續費: a NT$500 buy pays the NT$20 minimum fee — 4% of the
    trade, far outside the 1% band, so the fee-inclusive candidate is load-bearing."""
    _setup(conn)
    row = _row(conn, _draft("10", "50", "520"))
    assert row.fee == Decimal("20")
    assert not [i for i in row.issues if i.kind == "amount_mismatch"], row.issues


def test_a_net_sell_proceeds_total_is_consistent(conn: sqlite3.Connection) -> None:
    """淨收付 on a sell = 成交金額 − 手續費 − 交易稅, again with the engine's own fee/tax."""
    _setup(conn)
    # 10 × 50 = 500; fee floor 20; tax floor(500 × 0.3%) = 1 → net 479
    row = _row(conn, _draft("10", "50", "479", side=Side.SELL))
    assert not [i for i in row.issues if i.kind == "amount_mismatch"], (row.issues, row.fee,
                                                                        row.tax)


def test_no_stated_amount_means_no_check(conn: sqlite3.Connection) -> None:
    _setup(conn)
    row = _row(conn, _draft("100", "46", None))
    assert not [i for i in row.issues if i.kind == "amount_mismatch"]
    assert "stated_amount" not in row.payload and "amount_mismatch" not in row.payload


def test_the_tolerance_is_one_percent_and_both_edges_hold(conn: sqlite3.Connection) -> None:
    """The band is a named constant; its edge is inclusive and one step past it is not."""
    _setup(conn)
    assert Decimal("0.01") == STATED_AMOUNT_TOLERANCE
    # gross 100 × 100 = 10,000; buy fee floor(14.25) = 14 → raised to the NT$20 minimum, so
    # the candidates are 10,000 / 10,020 / 9,980 and the band is ±100 (1% of the gross)
    # around each: 10,120 is the inclusive edge and 10,121 is outside.
    inside = _row(conn, _draft("100", "100", "10120"))
    assert inside.fee == Decimal("20")
    assert not [i for i in inside.issues if i.kind == "amount_mismatch"]
    outside = _row(conn, _draft("100", "100", "10121"))
    assert [i for i in outside.issues if i.kind == "amount_mismatch"]


def test_the_stated_amount_never_reaches_the_commit_csv(conn: sqlite3.Connection) -> None:
    """It is EVIDENCE, not ledger data: the commit CSV's columns are unchanged, so the
    commit door re-derives exactly what it did before (AI-D18)."""
    _setup(conn)
    res = ai_agents_input(conn, "x", pool=_pool,
                          completer=_completer(_draft("100", "46", "50000")),
                          today=date(2026, 9, 23))
    header = res.csv_texts["transactions"].splitlines()[0]
    assert header.split(",") == _TXN_CSV_COLUMNS
    assert "stated_amount" not in _TXN_CSV_COLUMNS
    assert "50000" not in res.csv_texts["transactions"]
