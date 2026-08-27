"""Counter-evidence for wiring the MY benchmark: an index ticker takes no exchange suffix.

``yf_symbol`` appends the market suffix unconditionally (US "" · TW ".TW" · MY ".KL"), so a
non-US index routes to a ticker that does not exist — ``^KLSE`` with ``Market.MY`` would be
fetched as ``^KLSE.KL``. That is the documented reason ``benchmarks.py`` left MY without a
benchmark: "cheap BUT needs the yfinance suffix routing re-derived first".

The re-derivation: **a ``^``-prefixed ticker is a Yahoo INDEX, and Yahoo indices carry no
exchange suffix in any market** — ``^GSPC``, ``^KLSE``, ``^TWII``. This is not a guess about
``^KLSE``: ``pricing/index_source.py`` has been fetching exactly those three raw tickers for
the sentiment variable since before this change, bypassing ``yf_symbol`` entirely. The rule
was already true; ``yf_symbol`` simply did not know it, and ``^GSPC`` never exposed the gap
because the US suffix is empty anyway.
"""

from portfolio_dash.pricing.benchmarks import benchmark_for_market, get_benchmark
from portfolio_dash.pricing.index_source import INDEX_SYMBOLS
from portfolio_dash.pricing.providers.yfinance_provider import yf_symbol
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.shared.enums import Currency, Market


def test_an_index_ticker_keeps_its_own_name_in_every_market() -> None:
    for market in (Market.US, Market.TW, Market.MY):
        assert yf_symbol(InstrumentRef(symbol="^KLSE", market=market, board="")) == "^KLSE"
        assert yf_symbol(InstrumentRef(symbol="^GSPC", market=market, board="")) == "^GSPC"


def test_ordinary_symbols_still_get_their_suffix() -> None:
    """The new rule must not swallow the routing it sits in front of."""
    assert yf_symbol(InstrumentRef(symbol="2330", market=Market.TW, board="TWSE")) == "2330.TW"
    assert yf_symbol(InstrumentRef(symbol="8299", market=Market.TW, board="TPEx")) == "8299.TWO"
    assert yf_symbol(InstrumentRef(symbol="3182", market=Market.MY, board=".KL")) == "3182.KL"
    assert yf_symbol(InstrumentRef(symbol="AAPL", market=Market.US)) == "AAPL"


def test_the_rule_is_the_one_index_source_already_relies_on() -> None:
    """Not an assumption about ``^KLSE`` — the daily index job already fetches it raw."""
    assert "^KLSE" in INDEX_SYMBOLS
    for symbol in INDEX_SYMBOLS:
        assert symbol.startswith("^"), symbol


def test_my_now_has_a_benchmark_routed_to_the_right_ticker() -> None:
    bench = benchmark_for_market(Market.MY)
    assert bench is not None, "MY still has no benchmark"
    assert bench.storage_key == "^KLSE"
    assert bench.quote_ccy is Currency.MYR       # AI-D23: both legs in the local currency
    assert yf_symbol(bench.ref) == "^KLSE"       # …and it fetches as itself, not ^KLSE.KL
    assert get_benchmark(bench.key) is bench     # reachable by its API key too
