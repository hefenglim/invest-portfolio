import sqlite3
from collections.abc import Iterator

import pytest

from portfolio_dash.bootstrap import bootstrap_db


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)  # ledger + LLM tables (instruments incl. board)
    yield c
    c.close()
