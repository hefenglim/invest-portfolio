"""Options — final review R3, static facts of the web layer.

* **n2** — the cash statement tells an option CLOSE row (到期作廢／被指派／履約, or a refused
  close) by the server's §6.1 ``option_side`` label, never by coercing the server's price
  string to a number (``Number(r.price) !== 0``).
* **m1** — the dashboard re-scrolls to ``#options-panel`` once the section exists, because it is
  created after the dashboard fetch and the browser's own hash scroll has already run.
* **M1** — the option-close deep link has ONE reader (``pdPrefillOptionClose``): the boot-time
  ``?symbol=`` prefill stands aside when ``option_close`` is in the URL, and the prefill loads the
  TARGET account's holdings before it previews.
"""

from __future__ import annotations

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web"


def _src(name: str) -> str:
    return (_WEB / name).read_text(encoding="utf-8")


def _code(src: str) -> str:
    """The source without comments (the comments quote the banned expressions)."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def test_the_cash_statement_reads_the_close_row_from_the_server_label() -> None:
    code = _code(_src("cash.js"))
    assert not re.search(r"Number\(\s*r\.price\s*\)", code)
    describe = re.search(r"function describe\(r\) \{(.*?)\n  \}", code, re.S)
    assert describe, "web/cash.js lost describe()"
    assert "option_side" in describe.group(1)


def test_the_dashboard_scrolls_to_the_options_section_named_by_the_hash() -> None:
    code = _code(_src("options.js"))
    assert "location.hash" in code and "scrollIntoView" in code


def test_the_option_close_deep_link_has_one_reader() -> None:
    code = _code(_src("input.js"))
    assert "option_close" in code
    prefill = re.search(r"window\.pdPrefillOptionClose = async function \(p\) \{(.*?)\n  \};",
                        code, re.S)
    assert prefill, "web/input.js: pdPrefillOptionClose must await the target account's holdings"
    assert "await loadAcctHoldings(" in prefill.group(1)
