from decimal import Decimal
from pathlib import Path

import pandas as pd

from scripts.probe.adapters.yfinance_src import (
    has_raw_and_adj,
    max_decimals,
    parse_latest_close,
)

_FX = Path("tests/pricing/fixtures/yfinance/3182.KL.json")


def test_yf_parser_against_recorded_my_fixture() -> None:
    df = pd.read_json(_FX)
    close = parse_latest_close(df)
    assert close is None or isinstance(close, Decimal)
    assert close == Decimal(str(df["Close"].iloc[-1]))
    assert has_raw_and_adj(df)
    assert max_decimals(df) >= 0
