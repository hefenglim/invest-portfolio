"""R5 / QA-24: the 賣超 warning headline must be Traditional Chinese.

``validate.py`` emitted ``sell 150 > held 100`` and the UI renders it verbatim as the
HEADLINE of the confirmation dialog — directly above the Chinese sentence that explains
what confirming does (permanently discard the position's cost basis). It is also embedded
into ``api/routers/ledgers.py::_oversell_response``'s Chinese wrapper, so one sentence read
half in each language.

Both branches are covered: the net check and the DATE-AWARE branch (2026-07-31), whose
message already carried a Chinese tail bolted onto an English head.
"""

import re
import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.validate import TxnInput, validate_transaction
from portfolio_dash.shared.models.enums import Side

_CJK = re.compile(r"[一-鿿]")
_ASCII_ONLY = re.compile(r"^[\x00-\x7f]*$")


def _buy(conn: sqlite3.Connection, qty: str, on: str) -> None:
    conn.execute(
        "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
        "trade_date) VALUES ('tw_broker','2330','BUY',?,'100','0','0',?)", (qty, on))
    conn.commit()


def _sell(qty: str, on: date) -> TxnInput:
    return TxnInput(account_id="tw_broker", symbol="2330", side=Side.SELL,
                    quantity=Decimal(qty), price=Decimal("100"), trade_date=on)


def _oversell_message(conn: sqlite3.Connection, inp: TxnInput) -> str:
    issues = [i for i in validate_transaction(conn, inp)
              if i.kind == "sell_exceeds_holdings"]
    assert len(issues) == 1, issues
    return issues[0].message


def test_the_plain_oversell_headline_is_chinese(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    _buy(conn, "100", "2026-01-05")
    msg = _oversell_message(conn, _sell("150", date(2026, 6, 1)))
    assert not _ASCII_ONLY.match(msg), msg
    assert _CJK.search(msg), msg
    assert "sell " not in msg and "> held" not in msg, msg
    # The numbers are the point of the message; they must survive the translation.
    assert "150" in msg and "100" in msg, msg


def test_the_date_aware_oversell_headline_is_chinese(conn: sqlite3.Connection) -> None:
    """A back-dated sell covered only by a LATER buy: the branch with its own message."""
    seed_accounts(conn)
    _buy(conn, "100", "2026-01-05")
    _buy(conn, "900", "2026-05-01")
    msg = _oversell_message(conn, _sell("150", date(2026, 2, 1)))
    assert not _ASCII_ONLY.match(msg), msg
    assert _CJK.search(msg), msg
    # This branch already ENDED in Chinese, so "contains CJK" alone passed while the
    # HEADLINE — the part the dialog shows first — was still `sell 150 > held 100 on …`.
    assert "sell " not in msg and "> held" not in msg, msg
    assert "150" in msg and "100" in msg and "2026-02-01" in msg, msg
    # The net position (1000) is what makes this branch different from the one above —
    # the owner needs both numbers to see that the buy is simply dated after the sell.
    assert "1000" in msg, msg


def test_a_covered_sell_still_raises_nothing(conn: sqlite3.Connection) -> None:
    """Control: rewording a message must not change which rows trip the guard."""
    seed_accounts(conn)
    _buy(conn, "1000", "2026-01-05")
    kinds = {i.kind for i in validate_transaction(conn, _sell("800", date(2026, 6, 1)))}
    assert "sell_exceeds_holdings" not in kinds
