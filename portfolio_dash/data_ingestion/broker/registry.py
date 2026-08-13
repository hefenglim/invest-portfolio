"""Broker id -> adapter. The one place a second broker has to be mentioned.

The grouper, the reconciler and the ledger writers are broker-neutral by construction, so
adding a broker is one module plus one line here — never a change to anything downstream.
That is the point of the IR sitting between the two halves.

An unknown id raises rather than falling back to a "generic" adapter: broker exports differ
in column set, date format, sign convention and the meaning of their action codes, so a
generic parse of an unrecognised file produces a plausible, silently wrong ledger. Refusing
is the only safe default.
"""

from collections.abc import Callable, Mapping
from typing import Final

from portfolio_dash.data_ingestion.broker import schwab
from portfolio_dash.data_ingestion.broker.ir import RawEvent

#: ``(csv_text, *, source_file, aliases) -> events``. Every adapter has this shape.
Adapter = Callable[..., list[RawEvent]]

_ADAPTERS: Final[dict[str, Adapter]] = {
    # Both broker eras of the assessed export are the SAME format — the TDA-era rows differ
    # only in their description text ("TDA TRAN - …"), which schwab.py's rules already key
    # on. One adapter, not two.
    "schwab": schwab.parse,
}

#: Broker ids this build can read, for a CLI's ``--broker`` help and its validation.
BROKER_IDS: Final[tuple[str, ...]] = tuple(sorted(_ADAPTERS))


def get_adapter(broker: str) -> Adapter:
    """The adapter for *broker*; raises ``KeyError`` naming the ids that do exist."""
    try:
        return _ADAPTERS[broker]
    except KeyError:
        raise KeyError(
            f"unknown broker {broker!r} — known: {', '.join(BROKER_IDS)}. "
            "Add an adapter rather than parsing an unrecognised export as this one."
        ) from None


def parse_export(
    broker: str,
    csv_text: str,
    *,
    source_file: str,
    aliases: Mapping[str, str] | None = None,
) -> list[RawEvent]:
    """Parse one export with *broker*'s adapter."""
    return get_adapter(broker)(csv_text, source_file=source_file, aliases=aliases)
