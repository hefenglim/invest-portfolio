"""``shared/flags.py`` (PERF plan P5): prod defaults, read per call, loud on a bad value."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from portfolio_dash.shared import flags

_NAMES = ("PD_SETTLE", "PD_SETTLE_MODE", "PD_JOB_MODE", "PD_PREWARM")


@pytest.fixture(autouse=True)
def _unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with the four flags unset, whatever the suite's conftest sets."""
    for name in _NAMES:
        monkeypatch.delenv(name, raising=False)


def test_unset_reads_the_prod_defaults() -> None:
    assert flags.current() == flags.Flags(
        settle_enabled=True, settle_mode="child", job_mode="child", prewarm_enabled=True)
    assert {name: flags.raw(name) for name in _NAMES} == {
        "PD_SETTLE": "on", "PD_SETTLE_MODE": "child", "PD_JOB_MODE": "child", "PD_PREWARM": "1"}


def test_the_vocabulary_covers_exactly_the_four_flags() -> None:
    assert set(flags.ALLOWED) == set(_NAMES) == set(flags.PROD_DEFAULTS)
    for name, default in flags.PROD_DEFAULTS.items():
        assert default in flags.ALLOWED[name]


def test_every_value_is_read_on_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """No caching: a flip inside one test takes effect on the very next read."""
    monkeypatch.setenv("PD_SETTLE", "off")
    assert flags.settle_enabled() is False
    monkeypatch.setenv("PD_SETTLE", "on")
    assert flags.settle_enabled() is True

    monkeypatch.setenv("PD_SETTLE_MODE", "inline")
    assert flags.settle_mode() == "inline"
    monkeypatch.setenv("PD_SETTLE_MODE", "child")
    assert flags.settle_mode() == "child"

    monkeypatch.setenv("PD_JOB_MODE", "inline")
    assert flags.job_mode() == "inline"
    monkeypatch.delenv("PD_JOB_MODE")
    assert flags.job_mode() == "child"

    monkeypatch.setenv("PD_PREWARM", "0")
    assert flags.prewarm_enabled() is False
    monkeypatch.setenv("PD_PREWARM", "1")
    assert flags.prewarm_enabled() is True


def test_the_unit_test_combination_reads_back(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in (("PD_SETTLE", "on"), ("PD_SETTLE_MODE", "inline"),
                        ("PD_JOB_MODE", "inline"), ("PD_PREWARM", "0")):
        monkeypatch.setenv(name, value)
    assert flags.current() == flags.Flags(
        settle_enabled=True, settle_mode="inline", job_mode="inline", prewarm_enabled=False)


@pytest.mark.parametrize(("name", "value"), [
    ("PD_SETTLE", "ON"),          # exact match: no case folding
    ("PD_SETTLE", ""),            # empty is not "unset"
    ("PD_SETTLE", "true"),
    ("PD_SETTLE", " on"),         # no stripping either
    ("PD_SETTLE_MODE", "thread"),
    ("PD_JOB_MODE", "Child"),
    ("PD_PREWARM", "true"),
    ("PD_PREWARM", "yes"),
])
def test_an_invalid_value_raises_naming_the_variable_and_its_values(
        monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as caught:
        flags.raw(name)
    message = str(caught.value)
    assert name in message
    for allowed in flags.ALLOWED[name]:
        assert repr(allowed) in message or allowed in message
    with pytest.raises(ValueError):
        flags.current()               # the all-at-once read is just as loud


@pytest.mark.parametrize(("accessor", "name"), [
    (flags.settle_enabled, "PD_SETTLE"), (flags.settle_mode, "PD_SETTLE_MODE"),
    (flags.job_mode, "PD_JOB_MODE"), (flags.prewarm_enabled, "PD_PREWARM"),
])
def test_each_typed_accessor_validates(monkeypatch: pytest.MonkeyPatch,
                                       accessor: object, name: str) -> None:
    monkeypatch.setenv(name, "bogus")
    assert callable(accessor)
    with pytest.raises(ValueError, match=name):
        accessor()


def test_an_unknown_flag_name_is_a_key_error() -> None:
    with pytest.raises(KeyError):
        flags.raw("PD_NOT_A_FLAG")


def test_flags_imports_nothing_internal() -> None:
    """Every layer reads a flag, so the module may not import any package module."""
    tree = ast.parse(Path(flags.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0 and not (node.module or "").startswith("portfolio_dash"), \
                node.module
        elif isinstance(node, ast.Import):
            assert not any(a.name.startswith("portfolio_dash") for a in node.names)
