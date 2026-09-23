"""Account REFERENCE tokens for user-visible backend text (DEF-023, 2026-09-23).

The backend has no Traditional-Chinese account names: ``accounts.name`` is the English
config label (``Charles Schwab`` / ``TW Broker``), and the ONE naming authority the owner
reads is ``web/names.js`` (``pdNames.account(id)``, FU-D37 / G-01). Every backend sentence
that embedded ``accounts.name`` or a bare ``account_id`` therefore reached the screen in a
spelling no other surface used — the functional test of 2026-09-22 read 「tw_broker」 inside
the XIRR badge's reason (D-11) and 「TW Broker」 inside the overdraft message (DEF-008).

This module is the ONE seam that fixes the class rather than the instances:

* the backend writes a **token**, ``{account:<id>}``, wherever a sentence names an account
  (:func:`account_ref`); it never embeds a name, because it does not own one;
* the frontend's single fetch layer (``web/api.js``) walks every response — success bodies
  and error envelopes alike — and replaces each token with ``pdNames.account(id)``, so no
  page and no router has to know a token exists. Without ``names.js`` on the page the id
  itself is shown, which is what ``pdNames`` does for an unknown id too.

L0: stdlib only. Everything may import it.

The token grammar is deliberately minimal — braces, the literal word ``account``, a colon
and the id — so it cannot collide with money strings (Decimal text never contains ``{``),
with dates, or with any zh sentence, and so ``api.js`` can skip a response that does not
contain the literal ``{account:`` without running a regex over it.
"""

import re
from collections.abc import Callable

#: The token an account id is wrapped in. ``\{account:`` is what the fetch layer looks
#: for verbatim before it resolves anything.
ACCOUNT_REF_PREFIX = "{account:"

#: One regex for the backend side of the contract (tests and any server-side rendering
#: that has no ``names.js`` — none today). Account ids are ``[A-Za-z0-9_.-]``; the class is
#: wider than the seeded ids on purpose so a legacy id resolves too, and it excludes ``}``
#: so two adjacent tokens can never merge.
ACCOUNT_REF_RE = re.compile(r"\{account:([^{}\s]+)\}")


def account_ref(account_id: str) -> str:
    """The token for *account_id* — what every user-visible backend sentence embeds.

    >>> account_ref("tw_broker")
    '{account:tw_broker}'
    """
    return f"{ACCOUNT_REF_PREFIX}{account_id}}}"


def account_refs_in(text: str) -> list[str]:
    """Every account id referenced by *text*, in order of appearance (duplicates kept)."""
    return ACCOUNT_REF_RE.findall(text)


def resolve_account_refs(text: str, name_of: Callable[[str], str]) -> str:
    """Replace every token in *text* with ``name_of(id)``.

    The frontend does this in ``pdNames.resolveRefs``; this is the same operation for a
    Python caller (tests, or a future server-rendered surface such as a print report) so the
    grammar has one owner on each side of the wire.
    """
    if ACCOUNT_REF_PREFIX not in text:
        return text
    return ACCOUNT_REF_RE.sub(lambda m: name_of(m.group(1)), text)
