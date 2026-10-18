"""Corpus fixtures for the settlement-store tests (PERF plan W0.3).

``corpus_db(name)`` hands a test a PRIVATE, file-backed copy of one of the five reference
corpora (``tests/reference/corpora.py``) as ``(path, now, reporting)``. Templates are seeded
once per session and copied per call, so a test may write freely, and ``PRAGMA data_version``,
WAL and the shared-build key behave as on prod (never ``:memory:``).

    def test_something(corpus_db: CorpusFactory) -> None:
        path, now, reporting = corpus_db("golden")

``corpus_name`` parametrises a test over all five.
"""

from collections.abc import Callable
from itertools import count
from pathlib import Path

import pytest

from tests.reference.corpora import (
    CORPUS_NAMES,
    TEMPLATE_DIR_NAME,
    CorpusDB,
    copy_corpus,
)

CorpusFactory = Callable[[str], CorpusDB]


@pytest.fixture(scope="session")
def corpus_templates(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The session's template directory (the same one ``tests/reference`` uses)."""
    return tmp_path_factory.getbasetemp() / TEMPLATE_DIR_NAME


@pytest.fixture
def corpus_db(corpus_templates: Path, tmp_path: Path) -> CorpusFactory:
    """``corpus_db(name) -> CorpusDB(path, now, reporting)``: a fresh copy per call."""
    serial = count(1)

    def make(name: str) -> CorpusDB:
        dest = tmp_path / "corpora" / f"{name}-{next(serial)}.db"
        return copy_corpus(name, corpus_templates, dest)

    return make


@pytest.fixture(params=CORPUS_NAMES)
def corpus_name(request: pytest.FixtureRequest) -> str:
    """Each of the five corpus names in turn."""
    name: str = request.param
    return name
