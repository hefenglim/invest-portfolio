# Design: TW Board Resolution at Instrument Registration

- **Date:** 2026-06-10
- **Status:** Approved (design); pending spec review
- **Modules:** `shared/models/assets.py` (Instrument += board), `data_ingestion/` (store board r/w +
  a new `register.py`), `pricing/` (a new `board.py` probe). 
- **Depends on:** `shared/`, `pricing` providers (TwseProvider/TpexProvider) — but only via an
  **injected prober**, so `data_ingestion` does not statically import `pricing` (boundary preserved,
  mirroring `resolve`'s injected `llm_resolver`).
- **Follows:** `scheduler/` (which added the `instruments.board` column + reads it with a market-default
  fallback). This sub-project **populates** that column for TW.

## Context & purpose

The `scheduler/` work-list reads `instruments.board` to pick the right TW source/suffix (TWSE `.TW`
vs TPEx `.TWO`), falling back to a `TWSE` default when the column is empty. This sub-project fills the
column **once, at instrument registration**: when a TW symbol is first added (watchlist or holding),
probe TWSE then TPEx to guess its board, surface the guess for the user to confirm/correct, and store
the confirmed board on the `instruments` row — resolved permanently. US/MY boards are deterministic
(`""` / `.KL`) and set without a probe.

Discovery from the codebase: the transaction flows (`manual`/`csv`/`agents`) only **look up** existing
instruments via `resolve`; they do not create them, and `resolve`'s LLM fallback proposes but never
persists. So instrument **registration** (write a new `instruments` row) is the (largely unbuilt) home
for board resolution. This spec builds that backend; the listing/confirm **UI is `web_ui/`**.

## Decisions (settled 2026-06-10, human sign-off)

1. **`board` becomes a first-class `Instrument` attribute** (`Instrument.board: str = ""`); `store.py`'s
   `upsert_instrument`/`get_instrument`/`list_instruments` read & write it. Default `""` keeps existing
   `Instrument(...)` constructions working.
2. **Probe = TWSE then TPEx** (reusing `TwseProvider`/`TpexProvider`): if TWSE returns a quote → `"TWSE"`;
   else if TPEx returns one → `"TPEx"`; else `None`.
3. **Probe unavailable / not found → leave board empty + flag** `board_unresolved` (a soft,
   `needs_confirm` issue) for the user to set manually. It does **not** block registration; the
   work-list's `TWSE` fallback keeps quotes working meanwhile. No extra DB flag column — empty board is
   the "unresolved" state.
4. **Prober is injected** (`Callable[[str], str | None]`, default `None`) so `data_ingestion` stays
   decoupled from `pricing` and tests never hit the network. The default real prober lives in
   `pricing/board.py` and is wired in by the caller (web_ui later).
5. **US/MY boards are deterministic** (`""` / `.KL`), set without probing.

## Components

### `shared/models/assets.py`
- `Instrument` gains `board: str = ""`.

### `data_ingestion/store.py`
- `upsert_instrument` writes `board`; `get_instrument`/`list_instruments` SELECT + map `board`;
  `_row_to_instrument` includes it. (The `instruments.board` column already exists.)

### `pricing/board.py` (new)
- `probe_tw_board(symbol, *, twse=None, tpex=None) -> str | None`: builds an `InstrumentRef(symbol,
  Market.TW, board="")` (the TW providers ignore board), tries `twse.fetch_quote_latest([ref])` →
  `"TWSE"` on a hit, else `tpex.fetch_quote_latest([ref])` → `"TPEx"`, else `None`. Each provider call
  is wrapped so a network/HTTP error is treated as "not found here" (returns `None` overall — graceful,
  the user can retry or set manually). Providers default to fresh `TwseProvider()`/`TpexProvider()`;
  tests inject fakes.

### `data_ingestion/register.py` (new)
- `InstrumentDraft(instrument: Instrument, issues: list[Issue], written: bool)`.
- `register_instrument(conn, instrument, *, prober=None, confirm=False) -> InstrumentDraft`:
  - Determine board (only if `instrument.board` is empty — a caller-supplied/edited board is respected,
    no re-probe): US → `""`; MY → `".KL"`; TW → `prober(symbol)` if a prober is given, else unresolved.
  - If TW and the resulting board is empty → append a soft `Issue(kind="board_unresolved",
    needs_confirm=True, ...)`.
  - Produce `instrument.model_copy(update={"board": board})`.
  - If `confirm` and no **hard** issues → `upsert_instrument(conn, inst)`, `written=True`.
  - Return the draft (the web_ui calls with `confirm=False` to preview the guess, then `confirm=True`
    to write the user-confirmed/edited instrument — same pattern as `manual.enter_transaction`).
- A new issue kind `board_unresolved` (reuses the existing `validate.Issue` shape).

## Architecture / boundaries

- `data_ingestion/register.py` imports only `shared` + `data_ingestion` (store, validate); the prober is
  injected, so no `data_ingestion → pricing` import edge is introduced.
- `pricing/board.py` uses pricing's own providers (internal). The caller/composition root wires
  `register_instrument(prober=pricing.board.probe_tw_board)`.
- `Instrument.board` is consumed by `scheduler.build_worklist` (already, via SQL) and by `store.py`.

## Error handling / degradation

- Probe network/HTTP error or symbol-not-found → board `None`/empty + `board_unresolved` flag; never
  raises out of `register_instrument`. Registration still succeeds on confirm (empty board, flagged).

## Testing strategy (no live network)

- **Instrument model**: `board` defaults to `""`; round-trips through the model.
- **store**: `upsert_instrument`/`get_instrument`/`list_instruments` persist + read `board` (empty and
  set); existing instrument tests still pass with the defaulted field.
- **`probe_tw_board`** (pricing): inject fake TWSE/TPEx providers — TWSE hit → `"TWSE"`; only TPEx hit →
  `"TPEx"`; neither → `None`; a provider raising → treated as not-found (graceful), no exception out.
- **`register_instrument`** (data_ingestion): US → board `""` (no flag); MY → `".KL"`; TW with a fake
  prober → `"TWSE"`/`"TPEx"`; TW with prober `None` or returning `None` → empty board + `board_unresolved`
  issue; `confirm=True` upserts (board persisted, verify via `get_instrument`); `confirm=False` writes
  nothing; a pre-set `instrument.board` is respected (prober not called).
- No live network/providers in the suite (prober + providers are injected fakes).

## Out of scope (deferred / other modules)

- The **watchlist / holdings registration UI** that lists the guessed company/symbol/board and offers
  confirm/edit (`web_ui/`); this spec delivers the backend it calls.
- Wiring the real `pricing.board.probe_tw_board` into the live input flows (happens with the web_ui
  registration screen and/or the AI Agents Input confirm step).
- Re-resolving board for instruments already registered with an empty board (a later backfill /
  "resolve board" action in the settings or watchlist UI).
- Bulk/auto board resolution; per-symbol manual retry UX (web_ui).

## Designed-in flexibility

The injected prober keeps `data_ingestion` decoupled from `pricing` and makes the probe swappable (e.g.
a future TWSE/TPEx listing-file resolver instead of a quote probe) with no change to `register_instrument`.
`board` as a defaulted model field means every existing construction keeps working; new sources of board
(import files, manual entry) flow through the same `register_instrument`.

## Staging (the plan will sequence)

1. `Instrument.board` field (model) + `store.py` read/write of `board`.
2. `pricing/board.py` `probe_tw_board` (TWSE→TPEx, injectable, graceful).
3. `data_ingestion/register.py` `register_instrument` + `InstrumentDraft` + `board_unresolved` issue.
