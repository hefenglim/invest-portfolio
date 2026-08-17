from enum import StrEnum


class DataType(StrEnum):
    QUOTE_LATEST = "quote_latest"
    QUOTE_HISTORY = "quote_history"
    FX = "fx"
    DIVIDEND = "dividend"
    # Capability declaration ONLY (AI-D13, 2026-08-17): providers declare FUNDAMENTALS via
    # supports(), but it is deliberately ABSENT from DEFAULT_PROVIDER_ORDER — fundamentals
    # snapshots are a UNION (every enabled provider writes its own row), and the chain's
    # first-success-wins order would mean nothing. See rules/data-and-pricing.md (AI-D4/D14).
    FUNDAMENTALS = "fundamentals"
