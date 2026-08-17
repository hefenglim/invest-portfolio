"""Per-source fundamentals blocks (W3, AI-D13..D16, spec 2026-08-16-ai-assistant).

UNION, not a fallback chain (rules/data-and-pricing.md, AI-D4/D14): every *enabled*
provider writes its own ``external_snapshots`` row under ``dataset="fundamentals"`` and the
variable layer assembles one block per source — **no merge layer, never averaged**. The
block key IS the provenance.

Canonical field set (AI-D15) — fixed intersection; a provider that cannot supply a field
simply omits it (missing is honest, fabricated is not)::

    pe_ratio  pb_ratio  eps_ttm  market_cap
    dividend_yield_pct  beta  roe_pct  revenue_growth_yoy_pct

Units are unified AT THE SEAM: ``market_cap`` in the quote currency's raw units;
``*_pct`` fields are percents (``2.5`` = 2.5%); ratios are unitless. Every number is
parsed via ``Decimal(str(x))`` and stored as a canonical string, capped at 4 dp
ROUND_HALF_UP cap-only (the float-noise discipline of ``pricing/store``). Derived ratios
(yfinance leg) are computed HERE, at the fetch seam — the same precedent as
``consensus_source`` computing ``rating_score`` — so ``llm_insight`` reads verbatim and
computes nothing. A derived ratio with a dishonest denominator (``eps_ttm <= 0``,
``equity <= 0``) is OMITTED, not stored negative.

yfinance leg discipline: **never ``Ticker.info``** (the ``consensus_source.py`` rule — heavy
and fragile). It uses ``fast_info`` + the quarterly income statement + the balance sheet +
the dividends series, and derives what the keyed providers report directly.

All network I/O is isolated in the private ``_fetch_*`` seams so tests monkeypatch them
(the repo bans sockets in tests). Any failure/empty/garbage result degrades to ``None``
(no snapshot row), never an exception into the ingest loop and never a fabricated value.
"""

import os
from collections.abc import Callable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import requests
import yfinance as yf

from portfolio_dash.pricing.providers.yfinance_provider import yf_symbol
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.shared.enums import Currency, Market

DATASET = "fundamentals"
SOURCES: tuple[str, ...] = ("yfinance", "finnhub", "alphavantage")

#: The canonical 8 (AI-D15) — documented for tests + the variable's VarSpec example.
CANONICAL_FIELDS: tuple[str, ...] = (
    "pe_ratio", "pb_ratio", "eps_ttm", "market_cap",
    "dividend_yield_pct", "beta", "roe_pct", "revenue_growth_yoy_pct",
)

_CAP_DP = 4
_FINNHUB_METRIC_URL = "https://finnhub.io/api/v1/stock/metric"
_AV_URL = "https://www.alphavantage.co/query"
_TIMEOUT_S = 20

# Quote currency per market (the block's ``currency``; finnhub/AV are US-only so USD).
_MARKET_CCY = {Market.US: Currency.USD, Market.TW: Currency.TWD, Market.MY: Currency.MYR}


def _dec(value: object) -> Decimal | None:
    """``Decimal(str(value))`` if finite, else None — filters NaN/inf/None/garbage and
    Alpha Vantage's literal ``"None"``/``"-"`` placeholders."""
    if value is None or value in ("", "None", "-"):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _cap(v: Decimal) -> Decimal:
    """Round ``v`` to at most 4 dp; values within the cap are unchanged (cap-only)."""
    exp = v.as_tuple().exponent
    if isinstance(exp, int) and exp < -_CAP_DP:
        return v.quantize(Decimal(1).scaleb(-_CAP_DP), rounding=ROUND_HALF_UP)
    return v


def _clean(v: Decimal) -> str:
    """Canonical string for a stored field: capped, then cosmetic trailing zeros stripped.

    Decimal multiplication sums exponents, so ``0.015 x 100`` arrives as ``1.500`` — the
    value is exact, but the payload should not carry representation noise (the same
    discipline as the identity-factor short-circuit in ``pricing/store``). String-level
    strip (NOT ``normalize()`` — that turns ``40000`` into ``4E+4``).
    """
    s = str(_cap(v))
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _pct(ratio: Decimal | None) -> Decimal | None:
    """A provider-reported ratio (0.31) as a percent (31.0); None passes through."""
    return ratio * 100 if ratio is not None else None


def _ratio(num: Decimal | None, den: Decimal | None) -> Decimal | None:
    """``num / den``; None when either side is absent or ``den <= 0`` (no honest base)."""
    if num is None or den is None or den <= 0:
        return None
    return num / den


def _block(
    fields: dict[str, Decimal | None], *, currency: str, as_of: date
) -> dict[str, Any] | None:
    """The stored block: present fields as capped Decimal strings + currency/as_of.

    ``None`` when NO canonical field survived — an empty block writes no snapshot, so a
    covered-nothing symbol degrades honestly instead of storing a hollow payload.
    """
    out: dict[str, Any] = {
        name: _clean(v) for name, v in fields.items() if v is not None
    }
    if not out:
        return None
    out["currency"] = currency
    out["as_of"] = as_of.isoformat()
    return out


# --- yfinance leg (key-less, all three markets; never Ticker.info) --------------------
#
# The seams below return PLAIN structures (dicts/lists of Decimal-or-None) so the builder
# is pure and the pandas surface stays at the I/O edge:
#   fast_info       -> {"market_cap": …, "last_price": …, "currency": "USD", ...}
#   quarterly_income -> {line item: [values newest-first]}  (e.g. "Total Revenue": […])
#   balance_sheet    -> {line item: [values newest-first]}
#   dividends        -> [(ex_date, amount)] (any order; the builder windows them)


def _rows_newest_first(df: Any) -> dict[str, list[Decimal | None]]:
    """A yfinance statement DataFrame (items x timestamp columns) as item -> newest-first
    Decimal list. Column order in yfinance is already newest-first; we sort descending
    defensively so the builder's [0]/[4] indexing is stable across yfinance versions."""
    if df is None or getattr(df, "empty", True):
        return {}
    cols = sorted(getattr(df, "columns", []), reverse=True)
    out: dict[str, list[Decimal | None]] = {}
    for item in getattr(df, "index", []):
        out[str(item)] = [_dec(df.loc[item, c]) for c in cols]
    return out


def _fetch_yf_fast_info(symbol: str) -> dict[str, Any] | None:
    info = yf.Ticker(symbol).fast_info
    if info is None:
        return None
    try:
        return dict(info)
    except (TypeError, ValueError):  # pragma: no cover — defensive; fast_info is dict-like
        return None


def _fetch_yf_quarterly_income(symbol: str) -> dict[str, list[Decimal | None]]:
    return _rows_newest_first(yf.Ticker(symbol).quarterly_income_stmt)


def _fetch_yf_balance_sheet(symbol: str) -> dict[str, list[Decimal | None]]:
    return _rows_newest_first(yf.Ticker(symbol).balance_sheet)


def _fetch_yf_dividends(symbol: str) -> list[tuple[date, Decimal]]:
    series = yf.Ticker(symbol).dividends
    if series is None or getattr(series, "empty", True):
        return []
    out: list[tuple[date, Decimal]] = []
    for ts, amount in series.items():
        d = _dec(amount)
        if d is not None:
            out.append((ts.date(), d))
    return out


def _first(items: dict[str, list[Decimal | None]], names: tuple[str, ...]) -> Decimal | None:
    """The newest value of the first present line item among ``names``."""
    for name in names:
        values = items.get(name)
        if values:
            return values[0]
    return None


def _window_sum(
    items: dict[str, list[Decimal | None]], names: tuple[str, ...], n: int
) -> Decimal | None:
    """Sum of the newest ``n`` values of the first present item; None when FEWER than ``n``
    values exist (a partial TTM is not a TTM — never silently annualize a short window)."""
    for name in names:
        values = [v for v in items.get(name, []) if v is not None]
        if len(values) >= n:
            return sum(values[:n], Decimal(0))
    return None


def _info_get(info: dict[str, Any], *names: str) -> Decimal | None:
    """The first present, parseable fast_info value among key candidates.

    fast_info keys are camelCase in current yfinance (``lastPrice``/``marketCap``) but were
    snake_case in older versions — accept both so the leg survives a yfinance upgrade.
    """
    for name in names:
        d = _dec(info.get(name))
        if d is not None:
            return d
    return None


def build_yfinance_block(
    *,
    fast_info: dict[str, Any] | None,
    quarterly_income: dict[str, list[Decimal | None]],
    balance_sheet: dict[str, list[Decimal | None]],
    dividends: list[tuple[date, Decimal]],
    market: Market,
    as_of: date,
) -> dict[str, Any] | None:
    """Assemble the yfinance block from plain seam data (pure).

    Derived ratios follow the honest-denominator rule (``_ratio``): negative-EPS PE or
    negative-equity PB are omitted, not stored as misleading negatives. ``beta`` is not
    available without ``Ticker.info`` and stays absent by design.
    """
    info = fast_info or {}
    last_price = _info_get(info, "lastPrice", "last_price", "regularMarketPreviousClose")
    market_cap = _info_get(info, "marketCap", "market_cap")
    eps_ttm = _window_sum(quarterly_income, ("Diluted EPS", "Basic EPS"), 4)
    net_income_ttm = _window_sum(
        quarterly_income, ("Net Income", "Net Income Common Stockholders"), 4
    )
    equity = _first(
        balance_sheet,
        ("Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest"),
    )
    revenue = quarterly_income.get("Total Revenue", [])
    rev_latest = revenue[0] if revenue else None
    rev_year_ago = revenue[4] if len(revenue) >= 5 else None
    dividends_12m = sum(
        (amount for ex, amount in dividends if 0 <= (as_of - ex).days <= 366), Decimal(0)
    )
    fields = {
        "pe_ratio": _ratio(last_price, eps_ttm),
        "pb_ratio": _ratio(market_cap, equity),
        "eps_ttm": eps_ttm,
        "market_cap": market_cap,
        "dividend_yield_pct": (
            _pct(_ratio(dividends_12m, last_price)) if dividends_12m > 0 else None
        ),
        "beta": None,
        "roe_pct": _pct(_ratio(net_income_ttm, equity)),
        # Latest quarter vs the same quarter a year ago (5 quarterly columns back);
        # _ratio refuses a <= 0 or absent base, so a short history degrades to absent.
        "revenue_growth_yoy_pct": _pct(_ratio(
            rev_latest - rev_year_ago
            if rev_latest is not None and rev_year_ago is not None else None,
            rev_year_ago,
        )),
    }
    currency = str(info.get("currency") or _MARKET_CCY[market].value)
    return _block(fields, currency=currency, as_of=as_of)


def fetch_yfinance(
    ref: InstrumentRef, *, as_of: date, token: str | None = None
) -> dict[str, Any] | None:
    """Fetch + assemble one symbol's yfinance block, or None on any failure.

    ``token`` is accepted for dispatcher uniformity and ignored (yfinance needs no key).
    """
    del token
    sym = yf_symbol(ref)
    try:
        fast_info = _fetch_yf_fast_info(sym)
    except Exception:  # noqa: BLE001 — any source failure degrades to None
        fast_info = None
    try:
        quarterly_income = _fetch_yf_quarterly_income(sym)
    except Exception:  # noqa: BLE001
        quarterly_income = {}
    try:
        balance_sheet = _fetch_yf_balance_sheet(sym)
    except Exception:  # noqa: BLE001
        balance_sheet = {}
    try:
        dividends = _fetch_yf_dividends(sym)
    except Exception:  # noqa: BLE001
        dividends = []
    return build_yfinance_block(
        fast_info=fast_info, quarterly_income=quarterly_income,
        balance_sheet=balance_sheet, dividends=dividends,
        market=ref.market, as_of=as_of,
    )


# --- Finnhub leg (key-gated, US) -------------------------------------------------------
#
# GET /stock/metric?metric=all returns one ``metric`` block with TTM/annual metrics.
# Candidate key tuples tolerate finnhub's naming drift; the probe (tests/probe) pins what
# the live API actually returns. marketCapitalization is reported in MILLIONS — normalized
# to raw units here so ``market_cap`` means the same thing across every block.


def _fetch_finnhub_metric(symbol: str, token: str) -> dict[str, Any] | None:
    resp = requests.get(
        _FINNHUB_METRIC_URL,
        params={"symbol": symbol, "metric": "all", "token": token},
        timeout=_TIMEOUT_S,
    )
    resp.raise_for_status()
    metric = resp.json().get("metric")
    return dict(metric) if isinstance(metric, dict) and metric else None


def _pick(metric: dict[str, Any], names: tuple[str, ...]) -> Decimal | None:
    """The first present, parseable metric value among candidate keys."""
    for name in names:
        d = _dec(metric.get(name))
        if d is not None:
            return d
    return None


def build_finnhub_block(metric: dict[str, Any] | None, *, as_of: date) -> dict[str, Any] | None:
    """Assemble the finnhub block from the raw ``metric`` dict (pure).

    Provider-reported fields pass through as reported (their number, their basis — the
    block key keeps the provenance); only UNITS are normalized (millions -> raw,
    ratio -> percent already matches finnhub's percent convention).
    """
    if not metric:
        return None
    market_cap_m = _pick(metric, ("marketCapitalization",))
    fields = {
        "pe_ratio": _pick(metric, ("peTTM", "peBasicExclExtraTTM")),
        "pb_ratio": _pick(metric, ("pbAnnual", "pbQuarterly")),
        "eps_ttm": _pick(metric, ("epsTTM", "epsInclExtraItemsTTM", "epsBasicExclExtraItemsTTM")),
        # finnhub reports millions; raw units here. Sub-unit results are representation
        # noise — a market cap has no meaningful sub-unit precision, so whole units.
        "market_cap": (
            (market_cap_m * Decimal(1_000_000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            if market_cap_m is not None else None
        ),
        "dividend_yield_pct": _pick(
            metric, ("dividendYieldIndicatedAnnual", "currentDividendYieldTTM")
        ),
        "beta": _pick(metric, ("beta",)),
        "roe_pct": _pick(metric, ("roeTTM", "roeRfy")),
        "revenue_growth_yoy_pct": _pick(
            metric, ("revenueGrowthTTMYoy", "revenueGrowthQuarterlyYoy", "revenueGrowth3Y")
        ),
    }
    return _block(fields, currency=Currency.USD.value, as_of=as_of)


def fetch_finnhub(ref: InstrumentRef, *, as_of: date, token: str | None) -> dict[str, Any] | None:
    """Fetch + assemble one US symbol's finnhub block; None without a key or on failure."""
    resolved = token or os.environ.get("FINNHUB_KEY")
    if not resolved:
        return None
    try:
        metric = _fetch_finnhub_metric(ref.symbol, resolved)
    except Exception:  # noqa: BLE001 — any source failure degrades to None
        return None
    return build_finnhub_block(metric, as_of=as_of)


# --- Alpha Vantage leg (key-gated, US; OVERVIEW endpoint) ------------------------------
#
# OVERVIEW returns one flat dict; missing values arrive as the literal string "None".
# Its ratios (DividendYield, ReturnOnEquityTTM, QuarterlyRevenueGrowthYOY) are RATIOS
# (0.31 = 31%) — converted to percents here so ``*_pct`` means the same across blocks.


def _fetch_av_overview(symbol: str, token: str) -> dict[str, Any] | None:
    resp = requests.get(
        _AV_URL,
        params={"function": "OVERVIEW", "symbol": symbol, "apikey": token},
        timeout=_TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()
    # AV throttles with a 200 + {"Note"/"Information": ...} body instead of an error.
    if not isinstance(data, dict) or "Symbol" not in data:
        return None
    return data


def build_alphavantage_block(raw: dict[str, Any] | None, *, as_of: date) -> dict[str, Any] | None:
    """Assemble the AV block from the raw OVERVIEW dict (pure). Ratios -> percents."""
    if not raw:
        return None
    fields = {
        "pe_ratio": _dec(raw.get("PERatio")),
        "pb_ratio": _dec(raw.get("PriceToBookRatio")),
        "eps_ttm": _dec(raw.get("EPSTTM")),
        "market_cap": _dec(raw.get("MarketCapitalization")),
        "dividend_yield_pct": _pct(_dec(raw.get("DividendYield"))),
        "beta": _dec(raw.get("Beta")),
        "roe_pct": _pct(_dec(raw.get("ReturnOnEquityTTM"))),
        "revenue_growth_yoy_pct": _pct(_dec(raw.get("QuarterlyRevenueGrowthYOY"))),
    }
    return _block(fields, currency=Currency.USD.value, as_of=as_of)


def fetch_alphavantage(
    ref: InstrumentRef, *, as_of: date, token: str | None
) -> dict[str, Any] | None:
    """Fetch + assemble one US symbol's AV block; None without a key or on failure."""
    resolved = token or os.environ.get("ALPHAVANTAGE_KEY")
    if not resolved:
        return None
    try:
        raw = _fetch_av_overview(ref.symbol, resolved)
    except Exception:  # noqa: BLE001 — any source failure degrades to None
        return None
    return build_alphavantage_block(raw, as_of=as_of)


#: One source's fetch seam: (ref, as_of, token) -> the canonical block, or None.
FetchFn = Callable[..., dict[str, Any] | None]

#: Dispatcher for the union ingest: source id -> fetch seam. Keys match ``SOURCES`` and
#: the provider registry names (the same ids ``capable_ids`` returns).
FETCHERS: dict[str, FetchFn] = {
    "yfinance": fetch_yfinance,
    "finnhub": fetch_finnhub,
    "alphavantage": fetch_alphavantage,
}
