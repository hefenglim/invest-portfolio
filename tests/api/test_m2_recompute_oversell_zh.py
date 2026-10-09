"""I-2 — 重算's oversell refusal spoke English, under a code no other door uses.

``api/routers/actions.py`` answered ``error_body("oversell", str(exc))`` and ``str(exc)`` is
written for a developer by design: ``sell 9999 > held 10 for AAPL``. ``message`` is rendered
VERBATIM as a red toast (``window.toast(err.message, 'fail', err.code)``), so that sentence
went straight to the owner — the exact class of leak ``api/errors.py``'s zh rule exists to
stop, on the one door whose entire job is to tell the owner their ledger does not replay.

The same refusal already had a zh answer in three other places, all agreeing with each other:
``strategy/whatif.py`` (試算), ``api/routers/export.py`` (稅務套件) and, since wave 3, the
global handler in ``api/errors.py``. 重算 was the one that had its own wording AND its own
code (``"oversell"`` vs ``"oversold_position"``), so a frontend branch written for the other
three does not fire here.

Deliberately unchanged: the **status** stays 422 (this is a ledger state the owner must
resolve, not a malformed request), the replay stays STRICT, and ``str(exc)`` still travels —
in ``issues[].text``, where a developer can read it and the owner never sees it.

⚠ ``"oversell"`` remains the right code for a DIFFERENT door: ``api/routers/ledgers.py``'s
mutation guard uses it as a retryable-with-ack signal, and ``web/inbox.js`` /
``web/ledger.js`` branch on it (``err.code === 'oversell'`` → 「賣超確認」 → re-send with
``ack_oversell``). That contract is untouched — 重算 has no ack to offer.
"""

import re
import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.shared.models.enums import Side

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def _plant_oversell(conn: sqlite3.Connection) -> None:
    """Sell 9,999 of the golden 10-share AAPL position — an UNDECLARED oversell, inserted
    through the store helper (no soft-issue guard there), so the replay raises."""
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.SELL,
                       quantity=Decimal("9999"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 2, 1))
    conn.commit()


def _recompute(api_client: TestClient, golden_db: sqlite3.Connection) -> dict[str, Any]:
    _plant_oversell(golden_db)
    response = api_client.post("/api/actions/recompute", json={})
    assert response.status_code == 422, f"HTTP {response.status_code}: {response.text[:300]}"
    err: dict[str, Any] = response.json()["error"]
    return err


def test_the_code_is_the_one_every_other_door_uses(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """One vocabulary: 試算, 稅務套件 and the global handler all say ``oversold_position``."""
    assert _recompute(api_client, golden_db)["code"] == "oversold_position"


def test_the_message_is_zh_and_names_the_offending_row(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    message = str(_recompute(api_client, golden_db)["message"])
    assert _CJK.search(message), message
    assert "帳本中有賣超部位待釐清" in message
    assert "schwab" in message and "AAPL" in message and "2026-02-01" in message
    assert "無法重算" in message, message


def test_the_english_exception_text_never_reaches_the_owner(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """``message`` is a toast body. ``sell 9999 > held 10 for AAPL`` is not an answer."""
    err = _recompute(api_client, golden_db)
    assert "held" not in str(err["message"])
    assert "sell " not in str(err["message"])


def test_the_row_travels_as_fields_in_issues_exactly_as_the_other_doors_send_it(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Asserted as a WHOLE dict so it cannot drift from ``export.py`` / ``whatif.py``: one
    shape means one frontend branch renders all of them, and the English detail is kept
    where a developer can still read it."""
    issue: dict[str, Any] = _recompute(api_client, golden_db)["issues"][0]
    assert issue["sev"] == "error"
    assert issue["code"] == "oversold_position"
    assert issue["field"] is None
    assert issue["account_id"] == "schwab"
    assert issue["symbol"] == "AAPL"
    assert issue["trade_date"] == "2026-02-01"
    assert "held" in issue["text"], issue["text"]


def test_a_clean_ledger_still_recomputes(api_client: TestClient) -> None:
    """Control: nothing about the success path moves."""
    assert api_client.post("/api/actions/recompute", json={}).status_code == 200
