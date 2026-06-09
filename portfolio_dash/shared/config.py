"""Env-driven application settings (pydantic-settings)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from .enums import Currency


class Settings(BaseSettings):
    """Application settings, loaded from environment and ``.env``.

    Foundation fields only; DB-backed LLM config lives in ``shared/llm_config``.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Relative to the process cwd; set the DB_PATH env var for an absolute path.
    db_path: Path = Path("data/portfolio.db")
    app_env: Literal["dev", "prod"] = "dev"
    tz_display: str = "Asia/Taipei"  # display tz; storage is always UTC
    reporting_currency: Currency = Currency.TWD


@lru_cache
def get_settings() -> Settings:
    """Return the cached process-wide Settings singleton.

    The cache is populated on the first call and is never invalidated during normal
    operation, so later environment changes are not picked up. Call
    ``get_settings.cache_clear()`` in tests that need to re-read the environment.
    """
    return Settings()
