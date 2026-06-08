import sqlite3

from portfolio_dash.pricing.schema import create_tables


def test_create_tables_idempotent() -> None:
    c = sqlite3.connect(":memory:")
    create_tables(c)
    create_tables(c)  # second call must not error
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"prices", "fx_rates", "dividend_events"}.issubset(names)
