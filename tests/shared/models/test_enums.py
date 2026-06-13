from portfolio_dash.shared.models.enums import DividendType, Side


def test_side_members() -> None:
    assert {s.value for s in Side} == {"BUY", "SELL"}


def test_dividend_type_members() -> None:
    assert {d.value for d in DividendType} == {"CASH", "STOCK", "DRIP", "NET"}


def test_enums_are_str() -> None:
    assert Side.BUY == "BUY"
    assert DividendType.DRIP == "DRIP"
