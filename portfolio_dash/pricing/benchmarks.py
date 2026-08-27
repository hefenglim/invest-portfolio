"""Benchmark index registry for the dashboard TWR overlay (FU-D27).

The performance-comparison overlay pits the portfolio's time-weighted return against a
small, fixed set of market benchmarks. Benchmarks are NOT registered instruments — the
``prices`` table has no foreign key to ``instruments`` (``pricing/schema.py``) and nothing
joins the two, so a benchmark daily-close series can live in ``prices`` under a stable key
without any ``instruments``/watchlist row. This module is the single source of truth for
that key, the yfinance-routable ref, the quote currency, and the zh-TW label.

Adapter / storage-key decision (verified against
``pricing/providers/yfinance_provider.yf_symbol``):

  ``yf_symbol`` appends the market suffix — US ``""`` · TW ``".TW"`` · MY ``".KL"`` — and
  the registry stores every ``PriceRow`` under ``ref.symbol`` verbatim (it never renames).
  So the FETCH ticker and the STORAGE key are coupled through ``ref.symbol``. We therefore
  choose refs whose ``ref.symbol`` maps to the correct yfinance ticker AND is a stable,
  unambiguous ``prices.instrument`` key:

  - **0050 (元大台灣50, TW / TWD):** ``ref.symbol="0050"``, market=TW, board="TWSE" →
    yfinance ticker ``"0050.TW"`` (the correct TWSE ticker) → stored under
    ``prices.instrument = "0050"``.
  - **S&P 500 (^GSPC, US / USD):** ``ref.symbol="^GSPC"``, market=US, board="" → yfinance
    ticker ``"^GSPC"`` unchanged (US suffix is ``""``) → stored under
    ``prices.instrument = "^GSPC"``. yfinance serves ``^GSPC`` as the *true* index (the
    same ticker ``pricing/index_source.py`` already uses for the sentiment variable), so
    no ``SPY``-style proxy is needed and the series is a genuine index price-return series.

Collision conclusion (senior review): the key ``"0050"`` MAY equal a user-registered
symbol if the owner also holds 元大台灣50. That collision is **harmless**: the benchmark
ref and the user's instrument both resolve to the SAME yfinance ticker ``"0050.TW"`` and
write byte-identical ``PriceRow``s through the idempotent ``(instrument, as_of_date)``
upsert — the two writers can never disagree on a stored row. ``"^GSPC"`` cannot collide
(a ``"^"``-prefixed symbol is not a valid user instrument). Orphan benchmark rows (no
matching ``instruments`` row) are fine too: holdings come from the ledger, never from
``prices``, so a benchmark series is invisible to portfolio valuation.
"""

from pydantic import BaseModel

from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.shared.enums import Currency, Market


class Benchmark(BaseModel, frozen=True):
    """One comparison benchmark: API key, zh label, fetch/storage ref, quote currency."""

    key: str  # stable API/query key (``benchmark=<key>``)
    label: str  # zh-TW display label
    ref: InstrumentRef  # yfinance-routable AND the ``prices.instrument`` storage key
    quote_ccy: Currency  # the currency the benchmark's close is quoted in

    @property
    def storage_key(self) -> str:
        """The ``prices.instrument`` key this benchmark's series is stored under."""
        return self.ref.symbol


BENCHMARKS: tuple[Benchmark, ...] = (
    Benchmark(
        key="0050",
        label="元大台灣50",
        ref=InstrumentRef(symbol="0050", market=Market.TW, board="TWSE"),
        quote_ccy=Currency.TWD,
    ),
    Benchmark(
        key="sp500",
        label="S&P 500",
        ref=InstrumentRef(symbol="^GSPC", market=Market.US, board=""),
        quote_ccy=Currency.USD,
    ),
    Benchmark(
        key="klci",
        label="富時大馬 KLCI",
        ref=InstrumentRef(symbol="^KLSE", market=Market.MY, board=""),
        quote_ccy=Currency.MYR,
    ),
)


def benchmark_refs() -> list[InstrumentRef]:
    """The refs to fetch (daily history job + smart backfill), in registry order."""
    return [b.ref for b in BENCHMARKS]


def get_benchmark(key: str) -> Benchmark | None:
    """Look up a benchmark by its API key, or ``None`` for an unknown key."""
    for b in BENCHMARKS:
        if b.key == key:
            return b
    return None


#: Fixed market → benchmark map for prediction scoring (AI-D22, owner ruling 2026-08-19):
#: the yardstick for a ``relative`` prediction is chosen by the CODE from the instrument's
#: market, never by the LLM.
#:
#: MY was deliberately absent until 2026-08-27, because ``yf_symbol`` would have routed
#: ``^KLSE`` with ``Market.MY`` to ``^KLSE.KL`` — a ticker that does not exist. That routing
#: is now re-derived (a ``^``-prefixed Yahoo ticker is an index and takes no suffix in any
#: market), so KLCI is wired and every market has a yardstick. ``benchmark_for_market`` still
#: returns ``None`` for anything unmapped, and every caller must keep honouring it: the
#: degradation path is the honest answer for a market added later, not dead code.
_MARKET_BENCHMARK: dict[Market, str] = {
    Market.TW: "0050",
    Market.US: "sp500",
    Market.MY: "klci",
}


def benchmark_for_market(market: Market) -> Benchmark | None:
    """The fixed benchmark for a market's ``relative`` predictions, or ``None`` (AI-D22).

    ``None`` is the honest answer for a market with no wired benchmark (MY today): the
    caller turns it into ``pending_data``, never a guessed proxy.
    """
    key = _MARKET_BENCHMARK.get(market)
    return None if key is None else get_benchmark(key)
