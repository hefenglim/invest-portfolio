"""M9-02: the 額度加值 dialog collects a 備註 and must actually send it.

The dialog draws a 「備註（選填）」 field, the backend's ``TopupBody`` has accepted
``note: str | None`` since it was written, ``add_topup`` stores it, and the 加值歷史 table
draws a dedicated 備註 column. Only the POST body was missing the field, so every top-up
landed with ``note = None`` and the column was blank forever.

Nothing catches that shape: an omitted OPTIONAL field is a valid request, returns 200, and
the response body (``remaining_usd``) is correct — the money path was never wrong. The
only observable is a column that is always empty, which reads as "nobody writes notes".

The backend round trip is already covered (``test_llm_settings_api``); what was never
asserted is that the frontend fills the field it collects, so that is what is pinned here.
"""

import re
from pathlib import Path

_JS = Path(__file__).resolve().parents[2] / "web" / "settings-llm.js"

#: the POST call and its object-literal body, e.g. `api.post('/api/llm/quota/topup', { ... })`
_CALL = re.compile(
    r"\.post\(\s*'/api/llm/quota/topup'\s*,\s*(\{[^{}]*\})", re.S
)


def _topup_body() -> str:
    src = _JS.read_text(encoding="utf-8")
    found = _CALL.search(src)
    assert found is not None, (
        "no POST /api/llm/quota/topup call with an object-literal body found in "
        "web/settings-llm.js — has the call changed shape?"
    )
    return found.group(1)


def test_the_scan_finds_the_call_it_guards() -> None:
    """A guard that matches nothing passes forever."""
    assert "amount_usd" in _topup_body()


def test_topup_body_carries_the_note_field() -> None:
    body = _topup_body()
    assert "note" in body, (
        "加值對話框收集了備註卻沒有送出 — POST /api/llm/quota/topup body: " + body
    )


def test_the_note_comes_from_the_dialog_input() -> None:
    """A hard-coded or always-null note would pass the field check and still lose the text."""
    src = _JS.read_text(encoding="utf-8")
    assert re.search(r"noteIn\.value", src), (
        "the 備註 input's value is never read — the field would be sent empty"
    )
