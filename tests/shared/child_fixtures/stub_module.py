"""A test-only stub module: importing it patches ``stub_target`` (the ``PD_TEST_STUBS`` form)."""

from __future__ import annotations

from tests.shared.child_fixtures import stub_target

stub_target.VALUE = "stubbed"
