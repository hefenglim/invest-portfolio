from pathlib import Path

import pytest
from pydantic import ValidationError

from portfolio_dash.shared.config import Settings, get_settings
from portfolio_dash.shared.enums import Currency


def test_defaults() -> None:
    # _env_file=None isolates the test from any local .env on the dev box.
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.app_env == "dev"
    assert s.tz_display == "Asia/Taipei"
    assert s.reporting_currency == Currency.TWD
    assert isinstance(s.db_path, Path)
    # owner decision 2026-07-08: 5-year history backfill floor
    assert s.history_backfill_days == 1825


def test_history_backfill_days_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HISTORY_BACKFILL_DAYS", "1095")  # e.g. narrow to 3y
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.history_backfill_days == 1095


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("REPORTING_CURRENCY", "USD")
    # _env_file=None: rely solely on monkeypatched env, ignore any local .env.
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.app_env == "prod"
    assert s.reporting_currency == Currency.USD


def test_invalid_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    assert get_settings() is get_settings()
