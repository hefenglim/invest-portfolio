"""M4-03: a ``side`` that is not buy/sell is a DATA problem, not a server fault.

``ManualBody.side`` is a bare ``str`` with no validator, and ``api/wire.py::parse_side``
does ``Side(value.strip().upper())`` — a bare ``ValueError`` for anything else. Measured on
the running app: ``buy`` / ``BUY`` / ``Sell`` / ``"buy "`` answered 200, while ``hodl`` /
``""`` / ``" "`` / ``b`` / ``0`` / ``买`` all answered **HTTP 500**
``{"code":"internal_error","message":"系統發生未預期的錯誤，請稍後再試"}`` — the catch-all
in ``api/errors.py`` swallowing a typo.

The SAME bad value through the CSV door has always answered a sentence the owner can act
on, because ``csv_import._side_cell`` re-types the ``ValueError`` into a ``_CellError``. So
the fix does not write a new sentence: the manual door reuses that one, and the parity test
below is what stops the two forking later.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

_BAD_SIDE_ZH = "欄位 side 的內容「hodl」不是有效的買賣別（請填 BUY 或 SELL）"
_BLANK_SIDE_ZH = "必填欄位不可空白（欄位 side）"


def _body(side: str) -> dict[str, Any]:
    return {"account_id": "tw_broker", "symbol": "2330", "side": side,
            "date": "2026-06-11", "shares": "1000", "price": "600"}


@pytest.mark.parametrize("door", ["preview", "commit"])
def test_a_bad_side_never_500s_and_names_the_field(api_client: TestClient, door: str) -> None:
    r = api_client.post(f"/api/input/manual/{door}", json=_body("hodl"))
    assert r.status_code == 400, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert err["message"] == _BAD_SIDE_ZH


@pytest.mark.parametrize("blank", ["", " ", "\t"])
def test_a_blank_side_reads_as_a_blank_required_cell(
    api_client: TestClient, blank: str
) -> None:
    """A whitespace-only side is BLANK, not「內容「 」不是有效的買賣別」— the same reading the
    CSV door's ``_cell`` gives it, and the one that tells the owner what to do."""
    r = api_client.post("/api/input/manual/preview", json=_body(blank))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["message"] == _BLANK_SIDE_ZH


@pytest.mark.parametrize("bad", ["b", "0", "买", "SELLL", "sel"])
def test_every_other_bad_value_is_a_400_in_chinese(api_client: TestClient, bad: str) -> None:
    r = api_client.post("/api/input/manual/preview", json=_body(bad))
    assert r.status_code == 400, r.text
    message = r.json()["error"]["message"]
    assert bad in message and "買賣別" in message
    assert "系統發生未預期的錯誤" not in message


@pytest.mark.parametrize("good", ["buy", "BUY", "Sell", "sell", "buy ", " SELL"])
def test_the_accepted_spellings_are_unchanged(api_client: TestClient, good: str) -> None:
    """Counter-evidence: the fix must not narrow what already worked — case-insensitive and
    whitespace-tolerant, exactly as ``parse_side`` was."""
    r = api_client.post("/api/input/manual/preview", json=_body(good))
    assert r.status_code == 200, r.text


def test_the_manual_door_and_the_csv_door_say_the_SAME_sentence(
    api_client: TestClient,
) -> None:
    """The point of the fix: ONE owner for the sentence.

    A copy would drift the moment either side is re-worded, and「請填 BUY 或 SELL」is the
    kind of text that gets re-worded. Both doors are asked about the same bad cell and the
    two answers are compared, not eyeballed.
    """
    csv_text = ("account,symbol,side,date,shares,price\n"
                "tw_broker,2330,hodl,2026-06-11,1000,600\n")
    imported = api_client.post("/api/import/preview",
                               json={"kind": "transactions", "csv_text": csv_text})
    assert imported.status_code == 200, imported.text
    rows = imported.json()["rows"]
    assert rows and rows[0]["status"] == "error"
    csv_reason = rows[0]["reason"]

    manual = api_client.post("/api/input/manual/preview", json=_body("hodl"))
    assert manual.status_code == 400, manual.text
    assert manual.json()["error"]["message"] == csv_reason
