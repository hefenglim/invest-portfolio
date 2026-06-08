from enum import StrEnum


class DataType(StrEnum):
    QUOTE_LATEST = "quote_latest"
    QUOTE_HISTORY = "quote_history"
    FX = "fx"
    DIVIDEND = "dividend"
