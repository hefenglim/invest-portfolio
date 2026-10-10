"""DEF-065 on an UPGRADED database: the notify config is healed at boot, not by a GET.

``ops/notify.load_config`` self-heals a stored config that predates a rule in
``RULE_CATALOG`` (the rule defaults to subscribed) and persists the fix. Every caller reached
it through a read — ``GET /api/notify/config`` among them — so on a database written by an
older version the FIRST read of the 通知 settings page wrote ``notify_config``. DEF-065's class
guard boots a FRESH database, whose seed already carries every rule, so it could not see this.
Measured in the v0.1.29 release rehearsal: a v0.1.28 database booted on the release, then every
GET route called, left ``notify_config`` changed (``portfolio_drawdown`` / ``currency_weight``
added by the GET), while two boots in a row changed nothing.

The heal now runs in the lifespan (``notify.heal_config``), so a GET reads a complete config and
writes nothing.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api.app import create_app
from portfolio_dash.ops import notify
from portfolio_dash.shared.config import get_settings

_PREDATING = ("portfolio_drawdown", "currency_weight")


@pytest.fixture
def db_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    db = tmp_path / "portfolio.db"
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("PD_DISABLE_SCHEDULER", "1")
    get_settings.cache_clear()
    enable_socket()
    try:
        yield db
    finally:
        disable_socket(allow_unix_socket=True)
        get_settings.cache_clear()


def _row(db: Path) -> tuple[str, str]:
    with sqlite3.connect(db) as c:
        body, updated = c.execute(
            "SELECT body, updated_at FROM notify_config WHERE id = 1").fetchone()
    return str(body), str(updated)


def test_an_older_notify_config_is_healed_at_boot_and_the_get_writes_nothing(
    db_file: Path,
) -> None:
    with TestClient(create_app()):
        pass  # first boot: schema + seed, as any install has
    # The install predates two rules: drop them from the stored subscriptions.
    body, _ = _row(db_file)
    cfg = json.loads(body)
    for rid in _PREDATING:
        cfg["subscriptions"].pop(rid)
    with sqlite3.connect(db_file) as c:
        c.execute("UPDATE notify_config SET body = ?, updated_at = ? WHERE id = 1",
                  (json.dumps(cfg), "2026-08-09T00:00:00"))

    with TestClient(create_app()) as client:
        after_boot = _row(db_file)
        stored = json.loads(after_boot[0])["subscriptions"]
        assert all(stored.get(rid) is True for rid, _, _ in notify.RULE_CATALOG), (
            "the boot must heal the stored config")
        r = client.get("/api/notify/config")
        assert r.status_code == 200
        assert all(r.json()["subscriptions"][rid] for rid in _PREDATING)
        assert _row(db_file) == after_boot, "GET /api/notify/config wrote notify_config"
