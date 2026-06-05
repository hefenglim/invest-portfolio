"""Stable cross-cutting enums shared across all layers."""

from enum import StrEnum


class Currency(StrEnum):
    """Quote / settlement currencies handled by the system."""

    TWD = "TWD"
    USD = "USD"
    MYR = "MYR"


class Market(StrEnum):
    """Exchanges/markets where instruments trade."""

    US = "US"
    TW = "TW"
    MY = "MY"
