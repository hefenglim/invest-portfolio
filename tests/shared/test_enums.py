from portfolio_dash.shared.enums import Currency, Market


def test_currency_members_and_values() -> None:
    assert {c.value for c in Currency} == {"TWD", "USD", "MYR"}
    assert Currency.TWD.value == "TWD"


def test_currency_is_str_enum() -> None:
    assert Currency.USD == "USD"
    assert isinstance(Currency.USD, str)


def test_market_members_and_values() -> None:
    assert {m.value for m in Market} == {"US", "TW", "MY"}
    assert Market.US.value == "US"


def test_market_is_str_enum() -> None:
    assert Market.US == "US"
    assert isinstance(Market.US, str)
