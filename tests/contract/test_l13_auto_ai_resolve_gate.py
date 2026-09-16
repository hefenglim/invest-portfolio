"""L13 (demo audit; owner ruling 2026-09-16, 3(b)+(d)) — the automatic AI resolve is gated.

(b) a code-like miss (≤ 6 letters/digits, no name typed) never fires the paid resolver by
itself; (d) 設定 → AI 與額度 carries a switch, persisted as ``auto_ai_resolve`` in
``/api/ui-prefs``. The API half is exercised for real; the JS half is a static contract
(the guard is a plain regex + a pref read, and an e2e that types a typo would spend LLM
quota on the miss it exists to prevent).
"""

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from portfolio_dash.shared.ui_prefs import get_ui_prefs

_WEB = Path(__file__).resolve().parents[2] / "web"


def test_auto_ai_resolve_defaults_on_and_round_trips(api_client: TestClient) -> None:
    body = api_client.get("/api/ui-prefs").json()
    assert body["auto_ai_resolve"] is True and body["page_size"] == 50
    r = api_client.put("/api/ui-prefs", json={"auto_ai_resolve": False})
    assert r.status_code == 200, r.text
    assert r.json() == {"page_size": 50, "auto_ai_resolve": False}
    # Subset merge: changing the page size leaves the switch where it was.
    r = api_client.put("/api/ui-prefs", json={"page_size": 20})
    assert r.json() == {"page_size": 20, "auto_ai_resolve": False}
    assert api_client.get("/api/ui-prefs").json()["auto_ai_resolve"] is False


def test_an_empty_put_is_refused(api_client: TestClient) -> None:
    r = api_client.put("/api/ui-prefs", json={})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"


def test_a_pre_switch_table_is_migrated_in_place() -> None:
    """A database whose ui_prefs_config predates the column reads the default, not an error."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE ui_prefs_config (id INTEGER PRIMARY KEY CHECK (id = 1), "
        "page_size INTEGER NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute("INSERT INTO ui_prefs_config VALUES (1, 100, '2026-07-07T00:00:00')")
    conn.commit()
    assert get_ui_prefs(conn) == {"page_size": 100, "auto_ai_resolve": True}


def test_quick_add_gates_the_automatic_fire_on_the_pref_and_on_code_like_input() -> None:
    src = (_WEB / "inst-quickadd.js").read_text(encoding="utf-8")
    assert "api.get('/api/ui-prefs')" in src and "auto_ai_resolve" in src
    assert "if (!autoAiPref)" in src
    assert "/^[A-Za-z0-9.\\-]{1,6}$/.test(symIn.value.trim()) && !nameIn.value.trim()" in src
    # The automatic fire is still there for name-like input (R6-B kept, not reverted).
    assert "runAiResolve({ auto: true })" in src


def test_settings_page_persists_the_switch_through_ui_prefs() -> None:
    html = (_WEB / "settings.html").read_text(encoding="utf-8")
    assert 'id="pref-auto-ai"' in html and 'id="ai-resolve-panel"' in html
    js = (_WEB / "settings-llm.js").read_text(encoding="utf-8")
    assert "api.put('/api/ui-prefs', { auto_ai_resolve: next })" in js
