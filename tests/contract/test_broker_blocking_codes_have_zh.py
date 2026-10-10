"""Every BLOCKING code the broker reconciler can raise has a sentence on the import page.

``web/broker-import.js`` renders a blocking issue as ``BLOCK_ZH[code] || code``, so a code with
no entry reaches the owner as a bare Python identifier — QA-29b found ``suppressed_ref_unknown``
doing exactly that, after its sibling had been mapped and it had not. The set of codes is read
from ``reconcile.py`` itself, so a new check cannot ship without its sentence (the 2026-10-11
``reinvest_quantity_mismatch`` is the first to be held to it).
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_RECONCILE = _ROOT / "portfolio_dash" / "data_ingestion" / "broker" / "reconcile.py"
_PAGE = _ROOT / "web" / "broker-import.js"


def _blocking_codes() -> set[str]:
    src = _RECONCILE.read_text(encoding="utf-8")
    return set(re.findall(r'"([a-z_]+)",\s*"blocking"', src))


def _zh_keys() -> set[str]:
    src = _PAGE.read_text(encoding="utf-8")
    body = src[src.index("const BLOCK_ZH = {"):]
    body = body[:body.index("\n  };")]
    return set(re.findall(r"^\s{4}([a-z_]+):", body, re.M))


def test_every_blocking_reconcile_code_has_a_zh_sentence() -> None:
    missing = _blocking_codes() - _zh_keys()
    assert not missing, f"blocking codes rendered as a bare identifier: {sorted(missing)}"


def test_the_scan_sees_the_codes_it_guards() -> None:
    """Positive control: an empty scan would pass the test above vacuously."""
    codes = _blocking_codes()
    assert {"priced_row_mismatch", "reinvest_quantity_mismatch", "rows_lost"} <= codes
    assert {"priced_row_mismatch", "reinvest_quantity_mismatch"} <= _zh_keys()
