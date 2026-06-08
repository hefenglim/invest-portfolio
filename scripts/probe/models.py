"""Probe result model — one row per (source × data_type × market) measurement."""

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class DataType(StrEnum):
    QUOTE_LATEST = "quote_latest"
    QUOTE_HISTORY = "quote_history"
    FX = "fx"
    DIVIDEND = "dividend"


class Verdict(StrEnum):
    PRIMARY = "primary"
    FALLBACK = "fallback"
    UNUSABLE = "unusable"
    SKIPPED = "skipped"  # e.g. keyed source with no key supplied


class ProbeResult(BaseModel):
    source: str
    data_type: DataType
    market: str  # "US" | "TW" | "MY" | "FX"
    requires_key: bool
    verdict: Verdict

    batch_max: int | None = None
    rate_limit: str | None = None
    latency_ms: float | None = None
    coverage_hits: int = 0
    coverage_misses: list[str] = Field(default_factory=list)
    decimals_ok: bool | None = None
    has_raw_and_adj: bool | None = None
    history_earliest: str | None = None
    sample_value: Decimal | None = Field(default=None, allow_inf_nan=False)
    error: str | None = None
    notes: str | None = None
