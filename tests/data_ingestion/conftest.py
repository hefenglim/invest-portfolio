import sqlite3
from collections.abc import Iterator

import pytest

from portfolio_dash.data_ingestion.schema import create_tables


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    create_tables(c)
    yield c
    c.close()
