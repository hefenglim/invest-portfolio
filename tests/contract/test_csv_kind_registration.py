"""Every CSV import kind is registered at ALL of its registration points (audit F-28).

F-28 measured the cost of the current design: a new kind is **seven** edits, every one of
which fails quietly on its own — miss the router maps and the kind 400s as 「未知 kind」,
miss ``web/input.js`` and it exists but is unreachable, miss ``DATE_COLUMN_BY_KIND`` and
the import seam raises a ``KeyError`` on a date column that is not there.

There is deliberately no registry object here (the ``shared/ledger_registry.py`` treatment):
the seven points want genuinely different information — a parser, a builder, a writer, a
date column, an optional-column set, example rows, and a zh chip label — and collapsing
them would be a registry with seven heterogeneous fields, i.e. the same list with an extra
indirection. What this file adds instead is the property the registry buys: **forgetting one
is loud**.

The frontend halves are asserted by reading ``web/*.js`` as text. That is the only way: the
frontend has no build step and no module system to import, and the alternative — asserting
nothing — is what let the previous four kinds' registration points go uncounted.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from portfolio_dash.api.routers.input_center import _BUILDERS, _WRITERS
from portfolio_dash.data_ingestion.import_templates import (
    DATE_COLUMN_BY_KIND,
    OPTIONAL_COLUMNS,
    TEMPLATE_KINDS,
    annotated_columns,
    render_import_template,
    template_columns,
)

_WEB = Path(__file__).resolve().parents[2] / "web"


@pytest.mark.parametrize("kind", TEMPLATE_KINDS)
def test_backend_registration_points(kind: str) -> None:
    """The five backend points: builder, writer, date column, optional set, header+rows."""
    assert kind in _BUILDERS, f"{kind}: missing from input_center._BUILDERS"
    assert kind in _WRITERS, f"{kind}: missing from input_center._WRITERS"
    assert kind in DATE_COLUMN_BY_KIND, f"{kind}: missing from DATE_COLUMN_BY_KIND"
    assert kind in OPTIONAL_COLUMNS, f"{kind}: missing from OPTIONAL_COLUMNS"
    columns = template_columns(kind)
    assert columns, f"{kind}: empty header"
    assert DATE_COLUMN_BY_KIND[kind] in columns, (
        f"{kind}: date column {DATE_COLUMN_BY_KIND[kind]!r} is not one of {columns}")
    assert OPTIONAL_COLUMNS[kind] <= set(columns), (
        f"{kind}: optional columns name a column the header does not have")
    body = render_import_template(kind).split("\r\n")
    assert body[0] == ",".join(annotated_columns(kind))
    assert len([line for line in body[1:] if line]) >= 1, f"{kind}: no example rows"


@pytest.mark.parametrize("kind", TEMPLATE_KINDS)
def test_frontend_registration_points(kind: str) -> None:
    """The two frontend points: the kind chip (CSV_KINDS) and its column hint (CSV_HINTS).

    A kind missing from ``CSV_KINDS`` is fully implemented and completely unreachable —
    the failure F-28 calls out as silent.
    """
    src = (_WEB / "input.js").read_text(encoding="utf-8")
    kinds_line = re.search(r"const CSV_KINDS = \[(.+?)\];", src, re.S)
    assert kinds_line is not None, "CSV_KINDS not found in web/input.js"
    assert f"'{kind}'" in kinds_line.group(1), f"{kind}: missing from web/input.js CSV_KINDS"
    hints = re.search(r"const CSV_HINTS = \{(.+?)\n  \};", src, re.S)
    assert hints is not None, "CSV_HINTS not found in web/input.js"
    assert f"{kind}:" in hints.group(1), f"{kind}: missing from web/input.js CSV_HINTS"


def test_the_corporate_action_tab_is_wired_on_every_side() -> None:
    """Door 3 (§6.7): the 5th ledger tab exists in the page, the tab list, and the loader.

    Four separate places, because trades.html owns the tab bar, its inline glue owns the
    show/hide list and the export-kind map, and ledger.js owns the data. A tab present in
    three of the four renders an empty pane or exports the wrong ledger — both silent.
    """
    page = (_WEB / "trades.html").read_text(encoding="utf-8")
    ledger_js = (_WEB / "ledger.js").read_text(encoding="utf-8")
    assert 'id="tab-laction"' in page
    assert 'id="pane-laction"' in page
    assert 'id="action-body"' in page
    assert 'id="laction-pager"' in page
    assert 'id="action-add"' in page
    assert "'laction'" in page, "trades.html LEDGER_TABS is missing the 5th tab"
    assert "laction: 'actions'" in page, "the export-kind map is missing the 5th tab"
    assert "/api/ledgers/corporate-actions" in ledger_js
    assert "renderActions()" in ledger_js


def test_the_shared_form_is_loaded_on_both_door_pages() -> None:
    """One form, three doors (§6.7) — and door 2's page must already carry the script.

    ``web/detail.js`` (door 2) is a later one-liner ONLY if index.html already loads the
    component; otherwise adding that door is two edits again, and the second one is exactly
    the kind that gets forgotten.
    """
    for page in ("trades.html", "index.html"):
        html = (_WEB / page).read_text(encoding="utf-8")
        assert "corp-action-form.js" in html, f"{page}: does not load the shared form"
    form = (_WEB / "corp-action-form.js").read_text(encoding="utf-8")
    assert "window.pdCorpActionForm" in form
    # Door 1 opens the same component — not a near-copy of it (§6.7: one implementation,
    # three mount points).
    assert "pdCorpActionForm.open" in (_WEB / "input.js").read_text(encoding="utf-8")
    assert "pdCorpActionForm.open" in (_WEB / "ledger.js").read_text(encoding="utf-8")


def test_the_oversell_dialog_offers_the_repair_first() -> None:
    """§6.7 door 1: 補登公司行動 is listed BEFORE 確認為賣超, because 確認 discards the
    cost basis permanently and a missing corporate action is the likelier cause."""
    src = (_WEB / "input.js").read_text(encoding="utf-8")
    repair = src.index("補登公司行動")
    accept = src.index("確認為賣超")
    assert repair < accept, "the destructive option is offered before the repair"


def test_the_form_never_divides_the_two_ratio_terms() -> None:
    """trap #1 / §3.1(ii): the browser must never form the quotient.

    A ratio expressed as a single decimal is what turns a 2-for-7 of 700 shares into
    199.99 and discards the position's basis on the next sell. The form holds two integer
    boxes and posts two integer strings; the label the ledger table shows is composed
    server-side (`ratio_label`).
    """
    form = (_WEB / "corp-action-form.js").read_text(encoding="utf-8")
    ledger_js = (_WEB / "ledger.js").read_text(encoding="utf-8")
    for src, name in ((form, "corp-action-form.js"), (ledger_js, "ledger.js")):
        assert not re.search(r"ratio_to\s*/\s*", src), f"{name}: divides the ratio terms"
        assert not re.search(r"Number\(\s*\w*ratio", src), f"{name}: coerces a ratio to float"
