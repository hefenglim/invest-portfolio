import sqlite3
from collections.abc import Iterator

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.scheduler.jobs import create_scheduler_tables


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)  # ledger + LLM tables (instruments incl. board)
    create_scheduler_tables(c)  # scheduler tables (schedule_config, job_runs)
    yield c
    c.close()
