"""Printing rules shared by the owner-run scripts. One copy, because two would drift.

Both `verify_corporate_actions.py` and `schwab_convert.py` produce a report the owner is
expected to PASTE into a chat session, and both read files that are the owner's real
financial history. The rule they share is not a style preference — it is the reason those
reports can be shown to anyone at all:

    stdout carries fixed labels, integer counts, and machine issue *kinds*.
    It never carries an amount, a share count, an account name, or a file name.

A second copy of :func:`safe_symbol` would be a second copy of a security control, and the
copy nobody edits is the one that leaks. It lives here so there is exactly one.
"""

from __future__ import annotations

import re
import sys


def force_utf8_stdio() -> None:
    """Print zh-TW on a Windows console without a ``UnicodeEncodeError``.

    The owner runs these scripts on their own machine, which is Windows, where a
    piped/redirected stdout defaults to the ANSI code page (cp1252 here) — and every label
    they print is Traditional Chinese. Without this the run dies on its first line, or inside
    ``--help``, so **call it before ``parse_args``**. ``errors="replace"`` is the belt: a
    mangled label is a bad report, a traceback is no report at all.

    Documenting ``PYTHONIOENCODING=utf-8`` as a prerequisite was the alternative, and it is a
    fix that has to be remembered every time. A script that only works when its caller knows
    a trick does not work. (Caught by a test that runs the converter as a PROGRAM rather than
    importing it — the in-process tests all passed, because pytest hands them a UTF-8 stream.)
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")

# An equity ticker never contains whitespace, and never a digit-dot-digit run. An OPTION
# symbol contains both: Schwab writes contracts as ``TICKER MM/DD/YYYY STRIKE C|P``, e.g.
# ``TSLA 01/19/2024 200.00 P`` — and **the strike is an amount**.
#
# The discriminator is deliberately NOT "contains a digit". Two of this project's three
# markets quote numeric tickers — TW ``2330`` / ``0050``, MY ``3182`` — so a digit rule would
# mask most of the report on a TW or MY ledger while adding nothing on a US one. It is also
# not "contains a dot": US class shares are written ``BRK.B``. Whitespace or ``\d\.\d``
# catches the contract form and nothing that is a real ticker in any market this app serves.
_NOT_A_TICKER = re.compile(r"\s|\d\.\d")


def safe_symbol(symbol: str) -> str:
    """A symbol that is safe to print, and the reason this function has to exist.

    A privacy argument about TYPES — no ``Decimal`` reaches stdout — is **incomplete**
    (measured 2026-08-11). A symbol is a free string that the owner's own broker fills with a
    strike price. This is not contrived: in a real Schwab export the option contracts appear
    on the very journal dates the corporate actions do (the same date carries
    ``TICKER MM/DD/YYYY 7.50 P`` beside the equity), so an owner recording those actions has
    such a string as a ``from_symbol`` by construction.

    **Masked, not rejected.** Refusing an option-shaped symbol at the door would drop a row
    the owner supplied, which changes the counts and could turn a FAIL into a PASS —
    weakening a gate in order to protect its output. Masking changes only what is printed:
    every count, verdict and exit code is identical. The leading token is kept because it is
    the underlying ticker, which is what the owner needs in order to act.
    """
    if not _NOT_A_TICKER.search(symbol):
        return symbol
    head = symbol.split(maxsplit=1)[0] if symbol.split() else ""
    return f"{head} ⋯" if head and not _NOT_A_TICKER.search(head) else "⋯"
