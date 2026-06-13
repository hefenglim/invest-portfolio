"""Tests for `pricing/defaults.py::default_registry` token-getter wiring (review I-3).

Spec 14.2 requires providers read their API key from the ``data_sources`` DB table.
``default_registry(conn)`` must wire ``FinMindProvider`` with a DB-backed
``token_getter`` so a key set on the settings page takes effect on the next live
fetch; the zero-arg ``default_registry()`` form must stay backward-compatible
(env / ctor fallback) for existing callers and tests.
"""

import sqlite3
from collections.abc import Iterator

import pytest

from portfolio_dash.pricing import datasources_store
from portfolio_dash.pricing.defaults import default_registry
from portfolio_dash.pricing.providers.finmind_provider import FinMindProvider
from portfolio_dash.pricing.registry import Registry


@pytest.fixture
def ds_conn() -> Iterator[sqlite3.Connection]:
    """In-memory connection with the data_sources tables created (no seed needed)."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    datasources_store.create_tables(c)
    yield c
    c.close()


def _finmind(reg: Registry) -> FinMindProvider:
    provider = reg._providers["finmind"]
    assert isinstance(provider, FinMindProvider)
    return provider


def test_default_registry_with_conn_reads_finmind_token_from_db(
    ds_conn: sqlite3.Connection,
) -> None:
    """A FinMind key set in the DB is the token the live registry resolves."""
    datasources_store.set_api_key(ds_conn, "finmind", "db-token-xyz")
    reg = default_registry(ds_conn)
    assert _finmind(reg)._resolve_token() == "db-token-xyz"


def test_default_registry_with_conn_picks_up_db_change_at_call_time(
    ds_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The getter is read at resolve time, so a later DB write is seen (no rebuild)."""
    # Clear the env fallback so the getter alone determines the resolved token.
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)
    reg = default_registry(ds_conn)
    provider = _finmind(reg)
    assert provider._resolve_token() is None  # no key yet, no env fallback
    datasources_store.set_api_key(ds_conn, "finmind", "set-later")
    assert provider._resolve_token() == "set-later"  # DB write seen on next resolve


def test_default_registry_no_conn_is_backward_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero-arg form still builds and FinMind falls back to env/ctor (no DB getter)."""
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)
    reg = default_registry()
    assert isinstance(reg, Registry)
    assert _finmind(reg)._resolve_token() is None  # no getter, no env -> None


def test_default_registry_no_conn_finmind_uses_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a conn, FinMind still honours the FINMIND_TOKEN env fallback."""
    monkeypatch.setenv("FINMIND_TOKEN", "env-token")
    reg = default_registry()
    assert _finmind(reg)._resolve_token() == "env-token"
