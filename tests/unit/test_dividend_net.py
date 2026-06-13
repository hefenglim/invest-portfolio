from decimal import Decimal

from portfolio_dash.data_ingestion.dividend_model import apply_dividend_model
from portfolio_dash.shared.models.enums import DividendType


def test_dividend_type_has_net_member() -> None:
    assert DividendType.NET.value == "NET"


def test_apply_net_model_records_net_received() -> None:
    # MY single-tier: the recorded amount IS the net received; no withholding.
    out = apply_dividend_model("net", gross=Decimal("170"), net=Decimal("170"))
    assert out.gross == Decimal("170")
    assert out.withholding == Decimal("0")
    assert out.net == Decimal("170")
    assert out.reinvest_shares is None


def test_apply_net_defaults_net_to_gross_minus_withholding() -> None:
    out = apply_dividend_model("net", gross=Decimal("200"))
    assert out.net == Decimal("200") and out.withholding == Decimal("0")
