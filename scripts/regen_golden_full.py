"""Regenerate tests/golden/dashboard_full.json deliberately (spec-17 §17.6.1).

Run:  ./.venv/Scripts/python.exe scripts/regen_golden_full.py

Builds the rich spec-17 financial scenario (tests/contract/test_spec17_financials.py
:seed_full) through the REAL write paths, drives GET /api/dashboard with the frozen
clock, and writes the normalized payload as the regression snapshot.

The snapshot diff IS the contract-change review surface (§17.7.2): review the git diff
before committing, and only regenerate when a contract change is intended. NEVER run
this just to make the snapshot test go green.
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    # Hermetic settings: no scheduler, throwaway DB dir (no backups -> last_backup_at null),
    # mirroring tests/conftest.py::_safe_db so the snapshot matches the test environment.
    os.environ["PD_DISABLE_SCHEDULER"] = "1"
    os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="regen_golden_")) / "test.db")

    from portfolio_dash.api.app import create_app
    from portfolio_dash.api.deps import get_conn, get_now, get_reporting
    from portfolio_dash.shared.config import get_settings
    from portfolio_dash.shared.enums import Currency
    from tests.conftest import GOLDEN_NOW, init_golden_base
    from tests.contract.test_spec17_financials import seed_full

    get_settings.cache_clear()

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    seed_full(conn)
    conn.commit()

    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_now] = lambda: GOLDEN_NOW
    app.dependency_overrides[get_reporting] = lambda: Currency.TWD

    from fastapi.testclient import TestClient

    client = TestClient(app)
    body = client.get("/api/dashboard").json()
    conn.close()

    out = _ROOT / "tests" / "golden" / "dashboard_full.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
