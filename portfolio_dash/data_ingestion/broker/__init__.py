"""Broker-statement adapters: raw export -> broker-neutral IR -> the canonical ledgers.

A broker export and this project's CSV templates are at **different grains**. A template row
is one human intent, hand-editable in a spreadsheet. A broker row is one cash or share LEG,
and a single domain event spans up to three of them. Merging the two grains degrades both —
the human template acquires columns no human fills, and the broker path inherits a shape
that cannot express its own groups. So the templates are left alone and this package sits in
front of them::

    broker export ──▶ [adapter] ──▶ RawEvent stream ──▶ [grouper] ──▶ template rows / ledger
                      per broker                       broker-neutral

* :mod:`.ir` — the closed ``EventKind`` vocabulary and the frozen ``RawEvent``.
* :mod:`.schwab` — one broker's ``(action, description)`` classification.
* :mod:`.grouping` — folds multi-row groups (a DRIP triple, a paired journal) into events.
* :mod:`.registry` — broker id -> adapter, so a second broker costs one adapter and no core
  change.

Layering: this is ``data_ingestion``, which *"validates and normalizes into the canonical
transaction model before persisting and rejects bad input loudly"* (``architecture.md``).
Nothing here imports upward, and nothing here writes.
"""
