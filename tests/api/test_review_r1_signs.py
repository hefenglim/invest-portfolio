"""R1 counter-evidence: the sign guards the 2026-08-24 investment-logic review found missing.

``domain-ledger.md`` already states the law — *"Any ratio over the basis must divide by
``abs(cost_total)``: the basis is negative, so the bare ratio flips sign and shows a
profitable short as a loss"* — and ``unrealized_pct`` six lines away obeys it with a comment
explaining exactly why.  ``payback_ratio`` was written before the declared-short work and
never revisited, so the cross-account aggregate divides by a signed sum.

These tests fail on the pre-fix code.
"""

from decimal import Decimal

from portfolio_dash.api.routers.symbol import _aggregate_position
from portfolio_dash.portfolio.dashboard_models import HoldingRow
from portfolio_dash.shared.enums import Currency, Market

D = Decimal


def _row(account: str, *, shares: str, original: str, adjusted: str,
         short_open: bool = False) -> HoldingRow:
    orig, adj = D(original), D(adjusted)
    sh = D(shares)
    return HoldingRow(
        account_id=account, account_name=account, symbol="TSLA", name="Tesla",
        market=Market.US, sector="Auto", board="", quote_ccy=Currency.USD,
        shares=sh, original_avg=orig / sh, adjusted_avg=adj / sh,
        original_cost_total=orig, adjusted_cost_total=adj,
        dividend_portion=orig - adj,
        payback_ratio=(orig - adj) / abs(orig) if orig != D("0") else D("0"),
        short_open=short_open,
    )


def test_aggregate_payback_never_goes_negative_when_a_short_outweighs_the_long() -> None:
    """One account long with dividends, another holding an open short of the same symbol.

    Long leg: 1,000 cost, 300 of it already returned as cash dividends -> 回本進度 30%.
    Short leg: 5,000 of proceeds received, booked as a NEGATIVE basis (that is how an open
    short is represented, and every other formula depends on it).

    The signed sum of the two bases is -4,000.  Dividing the (positive) 300 by it prints
    **-7.5%** — a position that really did return 30% of its cost is rendered as having
    *un*-recovered money.  With ``abs()`` the ratio is small but never nonsensical.
    """
    rows = [
        _row("tw", shares="100", original="1000", adjusted="700"),
        _row("schwab", shares="-50", original="-5000", adjusted="-5000", short_open=True),
    ]
    agg = _aggregate_position(rows, None)
    assert agg is not None
    assert agg["dividend_portion"] == "300"
    assert Decimal(agg["payback_ratio"]) > 0, (
        f"回本進度 flipped sign on a signed basis sum: {agg['payback_ratio']}")
    assert Decimal(agg["payback_ratio"]) == D("300") / D("4000")


def test_aggregate_payback_unchanged_when_every_leg_is_long() -> None:
    """The guard must be inert on the ordinary book — abs() of a positive sum is itself."""
    rows = [
        _row("tw", shares="100", original="1000", adjusted="700"),
        _row("schwab", shares="50", original="3000", adjusted="3000"),
    ]
    agg = _aggregate_position(rows, None)
    assert agg is not None
    assert Decimal(agg["payback_ratio"]) == D("300") / D("4000")


# --- R1/⑨ fee_rule_snapshot provenance on broker-supplied rows ---------------------------

def test_broker_shaped_row_records_supplied_provenance_not_an_empty_snapshot() -> None:
    """A broker statement supplies BOTH fee and tax, so the auto-fill branch never fires.

    Until 2026-08-24 that left `fee_rule_snapshot` as `{}` on every broker-imported row —
    indistinguishable from "no rule was applied at all". 重算 never needed the snapshot
    (`cost_basis.py` reads `ev.fees`/`ev.tax` off the row), which is exactly why the silence
    went unnoticed: nothing computed wrong, the provenance just was not there.
    """
    import sqlite3

    from portfolio_dash.bootstrap import bootstrap_db
    from portfolio_dash.data_ingestion.config_seed import seed_accounts
    from portfolio_dash.data_ingestion.csv_import import build_transaction_preview
    from portfolio_dash.data_ingestion.store import upsert_instrument
    from portfolio_dash.shared.models.assets import Instrument

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="Apple"))
    # The exact shape data_ingestion/broker/convert.py emits: fee verbatim, tax a literal "0".
    csv_text = ("account,symbol,side,date,shares,price,fee,tax\n"
                "schwab,AAPL,BUY,2026-05-20,10,100,1.25,0\n")
    prev = build_transaction_preview(conn, csv_text)
    row = prev.rows[0]
    snap = {k[5:]: v for k, v in row.payload.items() if k.startswith("snap.")}
    assert snap, "broker-supplied row carried an empty fee_rule_snapshot"
    assert snap["engine"] == "supplied"
    assert snap["fee"] == "1.25" and snap["tax"] == "0"
    conn.close()


def test_partially_supplied_row_names_which_number_the_caller_gave() -> None:
    """Fee supplied, tax left to the engine: the snapshot describes the regime AND says so."""
    import sqlite3

    from portfolio_dash.bootstrap import bootstrap_db
    from portfolio_dash.data_ingestion.config_seed import seed_accounts
    from portfolio_dash.data_ingestion.csv_import import build_transaction_preview
    from portfolio_dash.data_ingestion.store import upsert_instrument
    from portfolio_dash.shared.models.assets import Instrument

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Tech", name="TSMC"))
    csv_text = ("account,symbol,side,date,shares,price,fee\n"
                "tw_broker,2330,SELL,2026-05-20,1000,100,999\n")
    prev = build_transaction_preview(conn, csv_text)
    snap = {k[5:]: v for k, v in prev.rows[0].payload.items() if k.startswith("snap.")}
    assert snap.get("supplied") == "fee", snap
    assert snap.get("engine") == "v2", snap   # the regime is still described
    conn.close()
