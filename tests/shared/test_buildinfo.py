"""Unit tests: shared.buildinfo — build identity resolution + graceful fallbacks.

``get_build_info`` is lru_cached, so every test clears the cache around it. The git
subprocess seam is exercised via monkeypatching ``_git`` (hermetic — the socket ban
does not cover subprocesses, but tests must not depend on the checkout's actual
HEAD/tag state, which changes every commit).
"""

import pytest

from portfolio_dash import __version__
from portfolio_dash.shared import buildinfo


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    buildinfo.get_build_info.cache_clear()
    monkeypatch.delenv("BUILD_COMMIT", raising=False)
    monkeypatch.delenv("BUILD_RELEASE", raising=False)


def test_tagged_head_reports_release(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git(*args: str) -> str | None:
        if args[0] == "rev-parse":
            return "abc1234"
        if args[0] == "describe":
            return "v9.9.9"
        return None

    monkeypatch.setattr(buildinfo, "_git", fake_git)
    info = buildinfo.get_build_info()
    assert info.version == __version__
    assert info.commit == "abc1234"
    assert info.release == "v9.9.9"


def test_non_tag_head_is_unreleased(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git(*args: str) -> str | None:
        if args[0] == "rev-parse":
            return "abc1234"
        return None  # describe --exact-match fails off-tag

    monkeypatch.setattr(buildinfo, "_git", fake_git)
    info = buildinfo.get_build_info()
    assert info.commit == "abc1234"
    assert info.release == "unreleased"


def test_no_git_degrades_to_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(buildinfo, "_git", lambda *a: None)
    info = buildinfo.get_build_info()
    assert info.commit == "unknown"
    assert info.release == "unreleased"
    assert info.version == __version__


def test_env_overrides_win(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(buildinfo, "_git", lambda *a: None)
    monkeypatch.setenv("BUILD_COMMIT", "deadbee")
    monkeypatch.setenv("BUILD_RELEASE", "v1.2.3")
    info = buildinfo.get_build_info()
    assert info.commit == "deadbee"
    assert info.release == "v1.2.3"


def test_result_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_git(*args: str) -> str | None:
        calls.append(args)
        return "abc1234" if args[0] == "rev-parse" else None

    monkeypatch.setattr(buildinfo, "_git", fake_git)
    first = buildinfo.get_build_info()
    second = buildinfo.get_build_info()
    assert first is second
    assert len(calls) == 2  # rev-parse + describe, once total
