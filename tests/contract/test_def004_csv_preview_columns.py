"""DEF-004 (functional test manual I-04, 2026-09-23): every import kind previews its OWN values.

The CSV preview had one header — # 日期 帳戶 買賣 代號 股數 價格 — written for trades and
reused by all six kinds. A 資金 template row therefore printed 「#1 2026-07-01 台灣券商 DEPOSIT
— — ✓ 可寫入」: the 600,000 TWD was in ``rows[].data.amount`` and no column asked for it. The
股利 gross, both 換匯 amounts, the 期初 build date and cost, and the 公司行動 ratio went dark
the same way.

``web/input.js`` now declares the columns PER KIND (``CSV_COLS``). This file is the class
guard: for each kind, the backend's own template is previewed through the real door, and
EVERY key the preview puts in ``data`` must be read by that kind's column block — or be named
in ``_NOT_A_COLUMN`` with a reason. A payload field added later therefore fails here instead
of going dark on screen. The key money/quantity fields the verifier listed are pinned by
name as well, so an over-broad allowlist cannot hide them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.import_templates import TEMPLATE_KINDS
from portfolio_dash.shared.cash_kinds import CASH_KIND_ZH

_WEB = Path(__file__).resolve().parents[2] / "web"
_INPUT_JS = _WEB / "input.js"

#: The fields the verifier found missing, per kind (DEF-004's own list) + the kind's date key.
_MUST_SHOW: dict[str, set[str]] = {
    "transactions": {"trade_date", "quantity", "price", "fee", "tax"},
    "dividends": {"date", "gross", "withholding", "net", "reinvest_shares"},
    "fx": {"date", "from_amount", "from_ccy", "to_amount", "to_ccy"},
    "openings": {"build_date", "shares", "original_cost_total"},
    "corporate_actions": {"date", "ratio_from", "ratio_to", "from_symbol", "to_symbol"},
    "cash": {"date", "amount", "ccy", "acq_home_amount"},
}
#: Payload keys deliberately NOT a preview column, with the reason. Keep this short.
_NOT_A_COLUMN: dict[str, dict[str, str]] = {
    "transactions": {
        "note": "free text; shown in the 交易 ledger after the commit, not needed to decide",
    },
}


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)(?<![:'\"])//[^\n]*$", "", src)


def _balanced(src: str, open_at: int) -> str:
    """The text from the bracket at ``open_at`` to its match, skipping string literals."""
    pairs = {"[": "]", "{": "}", "(": ")"}
    stack: list[str] = []
    i = open_at
    while i < len(src):
        c = src[i]
        if c in "'\"`":
            j = i + 1
            while j < len(src) and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            i = j + 1
            continue
        if c in pairs:
            stack.append(pairs[c])
        elif stack and c == stack[-1]:
            stack.pop()
            if not stack:
                return src[open_at:i + 1]
        i += 1
    raise AssertionError("unbalanced block")


def _column_blocks() -> dict[str, str]:
    src = _strip_comments(_INPUT_JS.read_text(encoding="utf-8"))
    m = re.search(r"const CSV_COLS = \{", src)
    assert m, "web/input.js lost CSV_COLS — the per-kind preview columns (DEF-004)"
    table = _balanced(src, m.end() - 1)
    blocks: dict[str, str] = {}
    for km in re.finditer(r"\n\s{4}(\w+): \[", table):
        blocks[km.group(1)] = _balanced(table, km.end() - 1)
    return blocks


def _reads(block: str) -> set[str]:
    return set(re.findall(r"\bd\.(\w+)", block))


def test_every_import_kind_has_its_own_columns() -> None:
    assert set(_column_blocks()) == set(TEMPLATE_KINDS)


@pytest.mark.parametrize("kind", sorted(_MUST_SHOW))
def test_the_verifiers_missing_fields_are_columns_now(kind: str) -> None:
    missing = _MUST_SHOW[kind] - _reads(_column_blocks()[kind])
    assert not missing, f"CSV preview for {kind} still does not show {sorted(missing)}"


@pytest.mark.parametrize("kind", sorted(TEMPLATE_KINDS))
def test_every_key_the_door_previews_reaches_a_column(
    kind: str, api_client: TestClient,
) -> None:
    """The template → the REAL preview door → every ``data`` key must be shown (or excused)."""
    tpl = api_client.get(f"/api/import/template?kind={kind}")
    assert tpl.status_code == 200
    pv = api_client.post("/api/import/preview",
                         json={"kind": kind, "csv_text": tpl.content.decode("utf-8-sig")})
    assert pv.status_code == 200, pv.text
    keys: set[str] = set()
    for row in pv.json()["rows"]:
        keys |= set(row.get("data") or {})
    assert keys, f"{kind}: the template previewed with no data at all — the guard is blind"
    excused = set(_NOT_A_COLUMN.get(kind, {}))
    dark = keys - _reads(_column_blocks()[kind]) - excused
    assert not dark, (
        f"{kind}: the preview door sends {sorted(dark)} and no CSV_COLS['{kind}'] column reads "
        "it — show it, or name it in _NOT_A_COLUMN with the reason")
    stale = excused - keys
    assert not stale, f"{kind}: _NOT_A_COLUMN excuses keys the door no longer sends: {stale}"


def test_the_date_column_reads_the_kinds_own_date_key() -> None:
    """期初's date is ``build_date``; reading ``date`` there printed 「—」 (DEF-004)."""
    first = {k: re.search(r"\[\s*'[^']+',\s*'[^']*',\s*\(d\) => f\.date\(d\.(\w+)\)", b)
             for k, b in _column_blocks().items()}
    got = {k: (m.group(1) if m else None) for k, m in first.items()}
    assert got == {
        "transactions": "trade_date", "dividends": "date", "fx": "date",
        "openings": "build_date", "corporate_actions": "date", "cash": "date",
    }


def _js_object(name: str) -> dict[str, str]:
    src = _INPUT_JS.read_text(encoding="utf-8")
    m = re.search(r"const " + name + r" = \{", src)
    assert m, f"web/input.js lost {name}"
    body = _balanced(src, m.end() - 1)
    return dict(re.findall(r"(\w+): '([^']*)'", body))


def test_the_display_label_copies_match_their_backend_owners() -> None:
    """The cash preview row carries a bare code; input.js keeps a display copy of the zh
    label map, held key for key to the owner so the copy cannot drift. The corporate-action
    copy is GONE (I-14, 2026-09-23): its preview rows carry the server's ``kind_label`` and
    the column prints that — ``test_i14_one_action_vocabulary.py`` fails on a new copy."""
    assert _js_object("CSV_CASH_KIND_ZH") == dict(CASH_KIND_ZH)
    src = _INPUT_JS.read_text(encoding="utf-8")
    assert "CSV_ACTION_KIND_ZH" not in src and "d.kind_label || d.kind" in src


def test_the_header_is_rendered_per_kind() -> None:
    html = (_WEB / "trades.html").read_text(encoding="utf-8")
    assert 'id="csv-head"' in html
    src = _INPUT_JS.read_text(encoding="utf-8")
    assert "$('#csv-head')" in src and "renderCsvHead(kind)" in src


def test_the_guard_can_see_a_dark_field() -> None:
    """Positive control: a block that forgot `gross` is caught by the same helpers."""
    block = "[ ['日期', 'num', (d) => f.date(d.date)], ['淨額', 'num', (d) => csvAmt(d.net)] ]"
    assert "gross" in _MUST_SHOW["dividends"] - _reads(block)


def test_the_preview_renders_with_the_requested_kinds_columns() -> None:
    """The table must be drawn with the columns of the kind the preview was made FOR — a
    renderer that fell back to one fixed table would pass every check above and still print
    「DEPOSIT — —」."""
    src = _strip_comments(_INPUT_JS.read_text(encoding="utf-8"))
    m = re.search(r"function renderCsvPreview\(preview, kind\) \{", src)
    assert m, "renderCsvPreview no longer takes the preview's kind"
    body = src[m.end():m.end() + 1200]
    assert re.search(r"const k = CSV_COLS\[kind\] \? kind : 'transactions';", body)
    assert "const cols = CSV_COLS[k];" in body
    assert "renderCsvPreview(resp, reqBody.kind)" in src
