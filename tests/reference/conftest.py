"""Fixtures for the v0.1.31 reference tests (PERF plan W0.2 / W0.3)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from pytest_socket import disable_socket, enable_socket

from tests.reference.corpora import TEMPLATE_DIR_NAME


@pytest.fixture(scope="session")
def corpus_template_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Where the session's corpus templates live (shared with ``tests/settlement``)."""
    return tmp_path_factory.getbasetemp() / TEMPLATE_DIR_NAME


@pytest.fixture
def sockets_enabled() -> Iterator[None]:
    """TestClient's anyio portal needs a socket pair on Windows (see ``api_client``)."""
    enable_socket()
    try:
        yield
    finally:
        disable_socket(allow_unix_socket=True)
