# Spec 12b — Input Center: CSV Import + AI Input (Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Add the bulk + AI write paths: `POST /api/import/{preview,commit}` (4 ledger kinds, two-phase, **commit re-derives from csv_text**) and `POST /api/input/ai/preview` (LLM text → preview + `meta`, degradation mapped to 402/409/503), over the existing importers + `ai_agents_input`.

**Architecture:** Thin routers in the existing `portfolio_dash/api/routers/input_center.py` (so 12b is sequential after 12a — same file). Reuses `data_ingestion` `build_*_preview` / `write_*_row` / `commit_preview` and `agents.ai_agents_input`. The AI backend is extended (decision D7) to return `(preview, meta)` with meta read back from the `llm_usage` row written during the call.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, sqlite3, Decimal, pytest + TestClient, mypy --strict, ruff. Gates via `./.venv/Scripts/python.exe -m ...`.

**Branch:** `feat/input-import-ai` (create off main @ spec 12a).

**Authoritative contract:** `docs/design-handoff/.../specs/12-input-center.md` §12.3–12.4.

## Decisions locked (2026-06-13)
- **D2 — commit re-derives from `csv_text`.** Commit body `{kind, csv_text, ack_warnings}`. The handler re-runs `build_<kind>_preview(conn, csv_text)` at commit time (re-validates against the CURRENT ledger — important: holdings may change between preview and commit), then commits non-hard-issue rows; if any soft-issue ("warn") row and `ack_warnings` is False → 422. This is safer than trusting client-sent rows and sidesteps the preview/commit shape round-trip.
- **D7 — extend backend for AI `meta`.** `ai_agents_input` returns `AiInputResult{preview, meta}` (+ a generated `csv_text` so AI can reuse `/api/import/commit`). `meta{model: str|None, via: "litellm", cost_usd: Decimal|None}` read from the latest `llm_usage` row for `agent="ai_agents_input"`. Existing `test_agents.py` updated to use `.preview`.

## Verified shapes
- Builders (all `(conn, csv_text) -> ImportPreview`): `csv_import.build_transaction_preview` (cols `account,symbol,side,date,shares,price`,+`fee,tax,note`), `dividend_import.build_dividend_preview` (`account,symbol,date,type,gross`,+...), `fx_import.build_fx_preview` (`account,date,from_ccy,from_amount,to_ccy,to_amount`), `opening_import.build_opening_preview` (`account,symbol,shares,original_avg_cost,build_date`,+`original_cost_total`). **All use column `account` (NOT `account_id`)** — tests use `account`; frontend-CSV header reconciliation is a wiring-phase follow-up.
- Writers (all `(conn, row: PreviewRow) -> int`): `write_transaction_row`, `write_dividend_row`, `write_fx_row`, `write_opening_row`.
- `preview.commit_preview(conn, preview: ImportPreview, *, accept: set[int], writer) -> ImportSummary{written: list[int], skipped: list[int]}`. `PreviewRow{index, raw, payload, fee, tax, issues}`; `.has_hard_issue` (any issue with `needs_confirm=False`).
- `agents.ai_agents_input(conn, text, *, completer=complete_structured) -> ImportPreview` (CURRENT). Catches `LLMError` → returns a 1-row preview whose issue `.kind` ∈ {`budget_exceeded`,`ai_not_activated`,`llm_unavailable`,`llm_error`}. `AiDraft{account_id, symbol, side: Side, date, shares, price, daytrade, is_etf, note}`.
- `llm_usage` columns: `ts, model, agent, input_tokens, output_tokens, cost` (cost is a string). Read latest: `SELECT model, cost FROM llm_usage WHERE agent=? ORDER BY rowid DESC LIMIT 1`.
- `api/wire.py`: `issue_wire`. Phase-0: `api/errors.error_body`, `fastapi.responses.JSONResponse`. Fixture `api_client`/`golden_db` (accounts seeded; tw_broker holds 2330 ×1000).

---

### Task 1: `POST /api/import/preview` + shared `_preview_wire` + kind maps

**Files:** Modify `portfolio_dash/api/routers/input_center.py`; Test `tests/contract/test_input_import_api.py`.

Preview response (spec 12.3): `{rows:[{n, status, reason, data}], summary:{total, ok, warn, error}}`. `status` = `"error"` if `has_hard_issue` else `"warn"` if any soft issue else `"ok"`. `reason` = first issue's message (or None). `data` = parsed display fields (use `row.payload` + fee/tax).

- [ ] **Step 1: failing test** — create `tests/contract/test_input_import_api.py`:
```python
from fastapi.testclient import TestClient

_TXN_CSV = (
    "account,symbol,side,date,shares,price\n"
    "tw_broker,2330,buy,2026-06-02,100,600\n"        # ok
    "tw_broker,2330,sell,2026-06-03,5000,600\n"      # warn: oversell
    "tw_broker,23300,buy,2026-06-02,100,600\n"       # error: unknown symbol
)


def test_import_preview_counts_and_status(api_client: TestClient) -> None:
    r = api_client.post("/api/import/preview", json={"kind": "transactions", "csv_text": _TXN_CSV})
    assert r.status_code == 200
    b = r.json()
    assert b["summary"] == {"total": 3, "ok": 1, "warn": 1, "error": 1}
    by_n = {row["n"]: row for row in b["rows"]}
    assert by_n[0]["status"] == "ok"
    assert by_n[1]["status"] == "warn" and "賣" in by_n[1]["reason"] or by_n[1]["status"] == "warn"
    assert by_n[2]["status"] == "error"


def test_import_preview_bad_kind_400(api_client: TestClient) -> None:
    r = api_client.post("/api/import/preview", json={"kind": "nope", "csv_text": "a,b\n1,2\n"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"
```

- [ ] **Step 2: run, expect fail (404).**

- [ ] **Step 3: implement** — add to `input_center.py`:
```python
from portfolio_dash.data_ingestion.csv_import import build_transaction_preview, write_transaction_row
from portfolio_dash.data_ingestion.dividend_import import build_dividend_preview, write_dividend_row
from portfolio_dash.data_ingestion.fx_import import build_fx_preview, write_fx_row
from portfolio_dash.data_ingestion.opening_import import build_opening_preview, write_opening_row
from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow, commit_preview

_BUILDERS = {
    "transactions": build_transaction_preview, "dividends": build_dividend_preview,
    "fx": build_fx_preview, "openings": build_opening_preview,
}
_WRITERS = {
    "transactions": write_transaction_row, "dividends": write_dividend_row,
    "fx": write_fx_row, "openings": write_opening_row,
}


def _row_status(row: PreviewRow) -> str:
    if row.has_hard_issue:
        return "error"
    return "warn" if row.issues else "ok"


def _row_data(row: PreviewRow) -> dict[str, Any]:
    data = dict(row.payload)
    if row.fee is not None:
        data["fee"] = str(row.fee)
    if row.tax is not None:
        data["tax"] = str(row.tax)
    return data


def _preview_wire(preview: ImportPreview) -> dict[str, Any]:
    rows = []
    counts = {"ok": 0, "warn": 0, "error": 0}
    for r in preview.rows:
        st = _row_status(r)
        counts[st] += 1
        rows.append({"n": r.index, "status": st,
                     "reason": r.issues[0].message if r.issues else None,
                     "data": _row_data(r)})
    return {"rows": rows, "summary": {"total": len(preview.rows), **counts}}


class ImportPreviewBody(BaseModel):
    kind: str
    csv_text: str


@router.post("/import/preview")
def import_preview(body: ImportPreviewBody, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
    builder = _BUILDERS.get(body.kind)
    if builder is None:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"未知 kind: {body.kind}", field="kind"))
    return _preview_wire(builder(conn, body.csv_text))
```

- [ ] **Step 4: run, expect pass.** **Step 5: gates + commit**
```bash
./.venv/Scripts/python.exe -m pytest -q ; ./.venv/Scripts/python.exe -m mypy portfolio_dash tests --strict ; ./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/api/routers/input_center.py tests/contract/test_input_import_api.py
git commit -m "feat(api): POST /api/import/preview (4 kinds, wire shape + summary) (spec 12.3)"
```

---

### Task 2: `POST /api/import/commit` (re-derive from csv_text)

**Files:** Modify `portfolio_dash/api/routers/input_center.py`; Test append.

Commit body `{kind, csv_text, ack_warnings}` → `{written, skipped}` (counts). Re-derive (D2); 422 when warn rows present and not acked; 400 bad kind.

- [ ] **Step 1: failing test** (append to `tests/contract/test_input_import_api.py`):
```python
def test_import_commit_writes_ok_rows(api_client: TestClient) -> None:
    csv = ("account,symbol,side,date,shares,price\n"
           "tw_broker,2330,buy,2026-06-02,100,600\n")
    r = api_client.post("/api/import/commit",
                        json={"kind": "transactions", "csv_text": csv, "ack_warnings": False})
    assert r.status_code == 200 and r.json() == {"written": 1, "skipped": 0}


def test_import_commit_warn_requires_ack_422(api_client: TestClient) -> None:
    r = api_client.post("/api/import/commit",
                        json={"kind": "transactions", "csv_text": _TXN_CSV, "ack_warnings": False})
    assert r.status_code == 422 and r.json()["error"]["code"] == "warnings_unacknowledged"


def test_import_commit_acked_writes_ok_and_warn_skips_error(api_client: TestClient) -> None:
    r = api_client.post("/api/import/commit",
                        json={"kind": "transactions", "csv_text": _TXN_CSV, "ack_warnings": True})
    # ok + warn written (2), error skipped (1)
    assert r.status_code == 200 and r.json() == {"written": 2, "skipped": 1}
```

- [ ] **Step 2: run, expect fail.**

- [ ] **Step 3: implement** — add to `input_center.py`:
```python
class ImportCommitBody(BaseModel):
    kind: str
    csv_text: str
    ack_warnings: bool = False


@router.post("/import/commit")
def import_commit(body: ImportCommitBody, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
    builder = _BUILDERS.get(body.kind)
    writer = _WRITERS.get(body.kind)
    if builder is None or writer is None:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"未知 kind: {body.kind}", field="kind"))
    preview = builder(conn, body.csv_text)  # re-derive (re-validate vs current ledger)
    has_warn = any((not r.has_hard_issue) and r.issues for r in preview.rows)
    if has_warn and not body.ack_warnings:
        return JSONResponse(status_code=422, content=error_body(
            "warnings_unacknowledged", "有警告列需確認後才寫入"))
    accept = {r.index for r in preview.rows if not r.has_hard_issue}
    summary = commit_preview(conn, preview, accept=accept, writer=writer)
    return {"written": len(summary.written), "skipped": len(summary.skipped)}
```

- [ ] **Step 4: run, expect pass.** **Step 5: gates + commit**
```bash
git add portfolio_dash/api/routers/input_center.py tests/contract/test_input_import_api.py
git commit -m "feat(api): POST /api/import/commit (re-derive from csv_text, ack-gated) (spec 12.3)"
```

---

### Task 3: AI input — extend `ai_agents_input` (meta) + `POST /api/input/ai/preview`

**Files:** Modify `portfolio_dash/data_ingestion/agents.py`, `portfolio_dash/api/routers/input_center.py`; Modify `tests/data_ingestion/test_agents.py` (return-type change); Test `tests/contract/test_input_ai_api.py`.

- [ ] **Step 1: failing tests** — create `tests/contract/test_input_ai_api.py` (inject a fake completer via the app dependency? No — the AI endpoint calls `ai_agents_input` with the default completer; tests monkeypatch the module-level `complete_structured` or pass a fake. Use monkeypatch on `agents.complete_structured` through the endpoint's call. Simplest: the endpoint calls `ai_agents_input(conn, text)` using the default; tests monkeypatch `portfolio_dash.data_ingestion.agents.complete_structured`).
```python
import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion import agents as agents_mod
from portfolio_dash.data_ingestion.agents import AiDraft, AiDraftList
from portfolio_dash.shared.llm_config import AINotActivated, LLMBudgetExceeded


def _fake_ok(*_a, **_k):  # noqa: ANN
    return AiDraftList(drafts=[AiDraft(account_id="tw_broker", symbol="2330", side="BUY",
                                       date="2026-06-02", shares="10", price="600")])


def test_ai_preview_ok(api_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents_mod, "complete_structured", _fake_ok)
    r = api_client.post("/api/input/ai/preview", json={"text": "在元大買 10 股 2330 @ 600"})
    assert r.status_code == 200
    b = r.json()
    assert b["summary"]["total"] == 1
    assert b["rows"][0]["data"]["symbol"] == "2330"
    assert "meta" in b and b["meta"]["via"] == "litellm"
    assert "csv_text" in b              # for reuse via /api/import/commit


def test_ai_preview_budget_402(api_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a, **_k):
        raise LLMBudgetExceeded("AI 額度用盡")
    monkeypatch.setattr(agents_mod, "complete_structured", _boom)
    r = api_client.post("/api/input/ai/preview", json={"text": "x"})
    assert r.status_code == 402 and r.json()["error"]["code"] == "budget_exceeded"


def test_ai_preview_not_activated_409(api_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a, **_k):
        raise AINotActivated("AI 未啟用")
    monkeypatch.setattr(agents_mod, "complete_structured", _boom)
    r = api_client.post("/api/input/ai/preview", json={"text": "x"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ai_not_activated"
```

- [ ] **Step 2: run, expect fail.**

- [ ] **Step 3: extend `agents.py`.** Add a result model + meta read-back; change `ai_agents_input` to return it. Add imports `from pydantic import BaseModel` (present), `from decimal import Decimal` (present). New:
```python
class AiMeta(BaseModel):
    model: str | None = None
    via: str = "litellm"
    cost_usd: Decimal | None = None


class AiInputResult(BaseModel):
    preview: ImportPreview
    meta: AiMeta
    csv_text: str = ""  # canonical CSV of the drafts, for reuse via /api/import/commit


def _latest_meta(conn: sqlite3.Connection) -> AiMeta:
    row = conn.execute(
        "SELECT model, cost FROM llm_usage WHERE agent='ai_agents_input' "
        "ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return AiMeta()
    return AiMeta(model=row["model"], cost_usd=Decimal(row["cost"]))


def _drafts_to_csv(drafts: list[AiDraft]) -> str:
    lines = ["account,symbol,side,date,shares,price,note"]
    for d in drafts:
        lines.append(f"{d.account_id},{d.symbol},{d.side.value},{d.date.isoformat()},"
                     f"{d.shares},{d.price},{d.note or ''}")
    return "\n".join(lines) + "\n"
```
Change `ai_agents_input` to return `AiInputResult`: on `LLMError` → `AiInputResult(preview=<degradation preview as today>, meta=AiMeta(), csv_text="")`; on success → build rows as today, then `AiInputResult(preview=ImportPreview(rows=rows), meta=_latest_meta(conn), csv_text=_drafts_to_csv(result.drafts))`.

- [ ] **Step 4: update `tests/data_ingestion/test_agents.py`** — wherever it calls `ai_agents_input(...)` and asserts on the returned `ImportPreview`, change to use `.preview` (e.g. `result = ai_agents_input(...); result.preview.rows`). Preserve each test's intent. Also any degradation test now checks `result.preview.rows[0].issues[0].kind` and `result.meta.model is None`.

- [ ] **Step 5: implement the AI endpoint** — add to `input_center.py`:
```python
from portfolio_dash.data_ingestion.agents import ai_agents_input

_LLM_HTTP = {"budget_exceeded": 402, "ai_not_activated": 409,
             "llm_unavailable": 503, "llm_error": 503}


class AiBody(BaseModel):
    text: str


@router.post("/input/ai/preview")
def ai_preview(body: AiBody, conn: sqlite3.Connection = Depends(get_conn)) -> Any:
    result = ai_agents_input(conn, body.text)
    # degradation: a single row carrying an LLM-error issue kind
    for r in result.preview.rows:
        for issue in r.issues:
            if issue.kind in _LLM_HTTP:
                return JSONResponse(status_code=_LLM_HTTP[issue.kind],
                                    content=error_body(issue.kind, issue.message))
    wire = _preview_wire(result.preview)
    wire["meta"] = {"model": result.meta.model, "via": result.meta.via,
                    "cost_usd": None if result.meta.cost_usd is None else str(result.meta.cost_usd)}
    wire["csv_text"] = result.csv_text
    return wire
```
> The endpoint calls `ai_agents_input(conn, text)` with the default completer; the tests monkeypatch `agents.complete_structured`, which `ai_agents_input` uses as its default — so the fake flows through. (If `ai_agents_input`'s default is bound at def-time, the monkeypatch on the module attribute still applies because the default is looked up via the module reference — verify; if not, the endpoint may need to pass `completer=` explicitly, but prefer not to.)

- [ ] **Step 6: run, expect pass** (ok preview + 402 + 409). Run full suite + `test_agents.py` (updated). **Step 7: gates + commit**
```bash
./.venv/Scripts/python.exe -m pytest -q ; ./.venv/Scripts/python.exe -m mypy portfolio_dash tests --strict ; ./.venv/Scripts/python.exe -m ruff check portfolio_dash tests
git add portfolio_dash/data_ingestion/agents.py portfolio_dash/api/routers/input_center.py tests/data_ingestion/test_agents.py tests/contract/test_input_ai_api.py
git commit -m "feat(api): POST /api/input/ai/preview + ai_agents_input meta (spec 12.4)"
```
> **Verify before coding:** confirm `ai_agents_input`'s `completer` default is resolved at call time via the module attribute (so `monkeypatch.setattr(agents_mod, "complete_structured", ...)` works). The current signature is `completer: Completer = complete_structured` (bound at def-time → monkeypatching the module name will NOT affect the bound default). **Therefore:** change `ai_agents_input` to default `completer: Completer | None = None` and inside do `completer = completer or complete_structured` (module lookup at call time), so tests monkeypatching `agents.complete_structured` take effect. Adjust existing `test_agents.py` calls accordingly (they pass a `completer=` explicitly, which still works).

---

### Task 4: CHANGELOG + full green

- [ ] **Step 1:** Append to `CHANGELOG.md` `[Unreleased] › ### Added` (after the spec-12a bullet):
```markdown
- **Input center — CSV import + AI input (spec 12b, Phase 1):** `POST /api/import/{preview,commit}`
  (4 ledger kinds; preview → `{rows:[{n,status,reason,data}],summary}`; **commit re-derives from
  `csv_text`** and re-validates vs the current ledger, ack-gating warn rows → 422) and
  `POST /api/input/ai/preview` (LLM text → preview + `meta`; degradation mapped
  `budget_exceeded`→402/`ai_not_activated`→409/`llm_unavailable`→503). `ai_agents_input` now
  returns `AiInputResult{preview, meta, csv_text}` (meta read from the `llm_usage` row). Completes
  the Phase-1 core data flow (specs 10/11/12).
```
- [ ] **Step 2:** `grep -c "^## \[v" CHANGELOG.md` → `1`.
- [ ] **Step 3:** `./.venv/Scripts/python.exe -m ruff check portfolio_dash tests && ./.venv/Scripts/python.exe -m mypy portfolio_dash tests --strict && ./.venv/Scripts/python.exe -m pytest -q` → clean, 0 failed.
- [ ] **Step 4:** `git add CHANGELOG.md && git commit -m "docs: CHANGELOG for CSV import + AI input (spec 12b)"`

## Self-review
- Coverage: 12.3 preview (T1) + commit re-derive (T2); 12.4 AI preview + meta + degradation mapping (T3). `_preview_wire` shared by import + AI. Reuses existing builders/writers/commit_preview/ai_agents_input.
- Decisions honored: D2 (commit re-derives from csv_text), D7 (backend extended for meta).
- Deferred/noted: CSV column header `account` (importers) vs frontend `account_id` → reconcile at wiring; AI commit reuses `/api/import/commit` via the returned `csv_text`; money-string unification (from 12a) still pending.
- The `completer` default binding fix (T3) is required for the monkeypatch tests to work — do not skip.
