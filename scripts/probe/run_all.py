# mypy: ignore-errors
"""Entrypoint: run all source probes live, write the comparison report."""

import datetime
import warnings
from decimal import Decimal
from pathlib import Path

from scripts.probe.adapters import (
    finmind_src,
    my_src,
    sentiment_src,
    tw_gov,
    twstock_src,
    us_alt,
    yfinance_src,
)
from scripts.probe.models import DataType, ProbeResult, Verdict
from scripts.probe.report import render_report
from scripts.probe.runner import run_probe

REPORT = Path("docs/probes/2026-06-08-data-source-probe-results.md")

# yfinance suffix mapping for batch download symbols.
_TW_LISTED_SUFFIX = ".TW"
_TW_OTC_SUFFIX = ".TWO"
_MY_SUFFIX = ".KL"

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)


def _yf_suffixed_tw(code: str) -> str:
    """TWSE-listed codes use ``.TW``; TPEx (OTC) codes use ``.TWO``."""
    return f"{code}{_TW_OTC_SUFFIX}" if code in tw_gov.OTC_TWO else f"{code}{_TW_LISTED_SUFFIX}"


def _batch_close(df, symbol: str) -> Decimal | None:
    """Pull the last non-NaN Close for ``symbol`` out of a multi-ticker download."""
    try:
        sub = df[symbol]
    except KeyError:
        return None
    closes = sub["Close"].dropna()
    if closes.empty:
        return None
    return Decimal(str(closes.iloc[-1]))


def _probe_yfinance_quote_latest(
    results: list[ProbeResult], market: str, codes: list[str], symbols: list[str]
) -> None:
    """Batch-download latest closes for ``symbols`` (mapped 1:1 to display ``codes``)."""
    state: dict[str, object] = {}

    def fetch() -> dict[str, object]:
        import yfinance as yf

        df = yf.download(
            " ".join(symbols), period="1d", auto_adjust=False, group_by="ticker", progress=False
        )
        hits: dict[str, Decimal] = {}
        misses: list[str] = []
        for code, sym in zip(codes, symbols, strict=True):
            close = _batch_close(df, sym)
            if close is not None:
                hits[code] = close
            else:
                misses.append(code)
        state["hits"] = hits
        state["misses"] = misses
        return state

    result = run_probe("yfinance", DataType.QUOTE_LATEST, market, fetch, requires_key=False)
    hits = state.get("hits", {})
    misses = state.get("misses", [])
    result.coverage_hits = len(hits)
    result.coverage_misses = misses
    result.batch_max = len(symbols)
    if hits:
        result.sample_value = next(iter(hits.values()))
    result.notes = (
        f"single yf.download() batch of {len(symbols)} symbols "
        f"({result.coverage_hits}/{len(symbols)} returned a close); "
        f"misses={misses or 'none'}"
    )
    results.append(result)


def _probe_yfinance_history(results: list[ProbeResult], market: str, symbol: str) -> None:
    state: dict[str, object] = {}

    def fetch() -> object:
        df = yfinance_src.fetch_history_df(symbol, period="5y")
        state["df"] = df
        return df

    result = run_probe("yfinance", DataType.QUOTE_HISTORY, market, fetch, requires_key=False)
    df = state.get("df")
    if df is not None and not df.empty:
        result.history_earliest = str(df.index[0].date())
        result.has_raw_and_adj = yfinance_src.has_raw_and_adj(df)
        result.coverage_hits = 1
        result.sample_value = yfinance_src.parse_latest_close(df)
        result.decimals_ok = None  # float64 columns -> noise, not true tick precision
        result.notes = (
            f"representative={symbol}, {len(df)} rows over 5y; decimals_ok left None — "
            "yfinance Close is float64 so max_decimals reflects float noise, not true "
            "market tick precision (see adapter note)"
        )
    results.append(result)


def _probe_yfinance_fx(results: list[ProbeResult]) -> None:
    state: dict[str, object] = {}

    def fetch() -> object:
        import yfinance as yf

        df = yf.download(
            " ".join(yfinance_src.FX), period="1d",
            auto_adjust=False, group_by="ticker", progress=False,
        )
        state["df"] = df
        return df

    result = run_probe("yfinance", DataType.FX, "FX", fetch, requires_key=False)
    df = state.get("df")
    if df is not None:
        hits: dict[str, Decimal] = {}
        misses: list[str] = []
        for pair in yfinance_src.FX:
            close = _batch_close(df, pair)
            if close is not None:
                hits[pair] = close
            else:
                misses.append(pair)
        result.coverage_hits = len(hits)
        result.coverage_misses = misses
        result.batch_max = len(yfinance_src.FX)
        if hits:
            result.sample_value = next(iter(hits.values()))
        result.notes = f"3 pairs in one batch: {hits}"
    results.append(result)


def _probe_yfinance_dividend(results: list[ProbeResult], symbol: str) -> None:
    state: dict[str, object] = {}

    def fetch() -> object:
        import yfinance as yf

        div = yf.Ticker(symbol).dividends
        state["div"] = div
        return div

    result = run_probe("yfinance", DataType.DIVIDEND, "US", fetch, requires_key=False)
    div = state.get("div")
    if div is not None and len(div):
        result.coverage_hits = 1
        result.sample_value = Decimal(str(div.iloc[-1]))
        result.history_earliest = str(div.index[0].date())
        result.notes = (
            f"representative={symbol}, {len(div)} dividend rows since "
            f"{result.history_earliest}; latest={result.sample_value}"
        )
    results.append(result)


def _probe_tw_gov_listed(results: list[ProbeResult]) -> None:
    yyyymmdd = datetime.date.today().strftime("%Y%m%d")
    sample = tw_gov.LISTED_TW[:4]
    state: dict[str, object] = {}

    def fetch() -> dict[str, str | None]:
        hits: dict[str, str] = {}
        misses: list[str] = []
        for code in sample:
            payload = tw_gov.fetch_twse_day(code, yyyymmdd)
            close = tw_gov.parse_twse_close(payload)
            if close:
                hits[code] = close
            else:
                misses.append(code)
        state["hits"] = hits
        state["misses"] = misses
        return hits

    result = run_probe("tw_gov (TWSE)", DataType.QUOTE_LATEST, "TW", fetch, requires_key=False)
    hits: dict[str, str] = state.get("hits", {})
    misses = state.get("misses", [])
    result.coverage_hits = len(hits)
    result.coverage_misses = misses
    result.batch_max = 1  # one stockNo per request — no batch endpoint
    if hits:
        sample_str = next(iter(hits.values())).replace(",", "")
        result.sample_value = Decimal(sample_str)
        result.decimals_ok = True
    result.notes = (
        f"STOCK_DAY per-stockNo calls for {len(sample)}/{len(tw_gov.LISTED_TW)} listed codes "
        f"on {yyyymmdd}: {hits} — string close preserves real tick decimals "
        "(e.g. '2,295.00'), unlike yfinance float64"
    )
    results.append(result)


def _probe_tw_gov_otc(results: list[ProbeResult]) -> None:
    state: dict[str, object] = {}

    def fetch() -> list[dict]:
        rows = tw_gov.fetch_tpex_daily()
        state["rows"] = rows
        return rows

    result = run_probe("tw_gov (TPEx)", DataType.QUOTE_LATEST, "TW", fetch, requires_key=False)
    rows = state.get("rows")
    if rows is not None:
        hits: dict[str, str] = {}
        misses: list[str] = []
        for code in tw_gov.OTC_TWO:
            close = tw_gov.tpex_close_for(rows, code)
            if close:
                hits[code] = close
            else:
                misses.append(code)
        result.coverage_hits = len(hits)
        result.coverage_misses = misses
        result.batch_max = len(rows)  # one request returns the whole mainboard
        if hits:
            result.sample_value = Decimal(next(iter(hits.values())))
            result.decimals_ok = True
        result.notes = (
            f"single mainboard_daily_close_quotes call ({len(rows)} rows); "
            f"{result.coverage_hits}/{len(tw_gov.OTC_TWO)} OTC_TWO codes found: {hits}; "
            f"misses={misses or 'none'} (likely emerging-board codes not on TPEx mainboard); "
            "string close preserves real tick decimals"
        )
    results.append(result)


def _probe_twstock(results: list[ProbeResult]) -> None:
    sample = tw_gov.LISTED_TW[:3]
    state: dict[str, object] = {}

    def fetch() -> dict[str, str | None]:
        hits: dict[str, str] = {}
        misses: list[str] = []
        for code in sample:
            payload = twstock_src.fetch_twstock_realtime(code)
            price = twstock_src.parse_twstock_price(payload)
            if price:
                hits[code] = price
            else:
                misses.append(code)
        state["hits"] = hits
        state["misses"] = misses
        return hits

    result = run_probe("twstock", DataType.QUOTE_LATEST, "TW", fetch, requires_key=False)
    hits: dict[str, str] = state.get("hits", {})
    misses = state.get("misses", [])
    result.coverage_hits = len(hits)
    result.coverage_misses = misses
    result.batch_max = 1
    if hits:
        result.sample_value = Decimal(next(iter(hits.values())))
    result.notes = (
        f"realtime.get() per code for {len(sample)} sample codes: {hits}; "
        "intraday/realtime source — useful as a latest-quote fallback alongside TWSE"
    )
    results.append(result)


def _probe_us_alt_stockprices(results: list[ProbeResult]) -> None:
    sample = yfinance_src.US[:3]
    state: dict[str, object] = {}

    def fetch() -> dict[str, object]:
        hits: dict[str, object] = {}
        misses: list[str] = []
        for sym in sample:
            payload = us_alt.fetch_stockprices(sym)
            price = us_alt.parse_stockprices_close(payload)
            if price is not None:
                hits[sym] = price
            else:
                misses.append(sym)
        state["hits"] = hits
        state["misses"] = misses
        return hits

    result = run_probe("stockprices.dev", DataType.QUOTE_LATEST, "US", fetch, requires_key=False)
    hits: dict[str, object] = state.get("hits", {})
    misses = state.get("misses", [])
    result.coverage_hits = len(hits)
    result.coverage_misses = misses
    result.batch_max = 1
    if hits:
        result.sample_value = Decimal(str(next(iter(hits.values()))))
    result.rate_limit = "observed 429 Too Many Requests on follow-up calls in discovery"
    result.notes = (
        f"latest-only, no key, per-symbol GET for {len(sample)} sample tickers: {hits}; "
        "aggressive throttling observed during adapter discovery (429s) — fallback, not "
        "primary, for US latest quotes (yfinance remains primary)"
    )
    results.append(result)


def _probe_my_klsescreener(results: list[ProbeResult]) -> None:
    sample = my_src.MY[:2]
    state: dict[str, object] = {}

    def fetch() -> dict[str, str | None]:
        hits: dict[str, str] = {}
        misses: list[str] = []
        for code in sample:
            html = my_src.fetch_klse_html(code)
            price = my_src.parse_klse_price(html)
            if price:
                hits[code] = price
            else:
                misses.append(code)
        state["hits"] = hits
        state["misses"] = misses
        return hits

    result = run_probe("klsescreener", DataType.QUOTE_LATEST, "MY", fetch, requires_key=False)
    hits: dict[str, str] = state.get("hits", {})
    misses = state.get("misses", [])
    result.coverage_hits = len(hits)
    result.coverage_misses = misses
    result.batch_max = 1
    if hits:
        result.sample_value = Decimal(next(iter(hits.values())))
        result.decimals_ok = True
    result.notes = (
        f"scraped #price data-value per code for {len(sample)} sample codes: {hits}; "
        "returns 3-dp STRING (e.g. '2.260') — true Bursa tick precision, corroborates "
        "yfinance's MY latest close (which loses sub-pip precision to float64)"
    )
    results.append(result)


def _probe_my_malaysiastock(results: list[ProbeResult]) -> None:
    """Malaysiastock.biz — secondary MY 3-dp string source (spec 20.8 redundancy)."""
    sample = my_src.MY[:2]
    state: dict[str, object] = {}

    def fetch() -> dict[str, str | None]:
        hits: dict[str, str] = {}
        misses: list[str] = []
        for code in sample:
            html = my_src.fetch_malaysiastock_html(code)
            price = my_src.parse_malaysiastock_price(html)
            if price:
                hits[code] = price
            else:
                misses.append(code)
        state["hits"] = hits
        state["misses"] = misses
        return hits

    result = run_probe("malaysiastock", DataType.QUOTE_LATEST, "MY", fetch, requires_key=False)
    hits: dict[str, str] = state.get("hits", {})
    misses = state.get("misses", [])
    result.coverage_hits = len(hits)
    result.coverage_misses = misses
    result.batch_max = 1
    if hits:
        result.sample_value = Decimal(next(iter(hits.values())))
        result.decimals_ok = True
    result.notes = (
        f"scraped #SharePrice per code for {len(sample)} sample codes: {hits}; "
        "secondary 3-dp STRING source alongside klsescreener (single-source redundancy)"
    )
    results.append(result)


def _probe_sentiment_index(results: list[ProbeResult]) -> None:
    """VIX + CNN Fear & Greed + the three benchmark indices (key-less; spec 20.7)."""
    # VIX (yfinance ^VIX).
    vix_state: dict[str, object] = {}

    def fetch_vix() -> object:
        close = sentiment_src.fetch_yf_close(sentiment_src.VIX_SYMBOL)
        vix_state["close"] = close
        return close

    vix = run_probe("yfinance (VIX)", DataType.QUOTE_LATEST, "SENTIMENT",
                    fetch_vix, requires_key=False)
    close = vix_state.get("close")
    if close is not None:
        vix.sample_value = Decimal(str(close))
    vix.coverage_hits = 1 if close is not None else 0
    vix.notes = f"^VIX last close = {close}"
    results.append(vix)

    # CNN Fear & Greed.
    fng_state: dict[str, object] = {}

    def fetch_fng() -> object:
        parsed = sentiment_src.parse_fng(sentiment_src.fetch_cnn_fng())
        fng_state["fng"] = parsed
        return parsed

    fng = run_probe("cnn_fng", DataType.QUOTE_LATEST, "SENTIMENT", fetch_fng,
                    requires_key=False)
    parsed = fng_state.get("fng")
    fng.coverage_hits = 1 if parsed else 0
    fng.notes = f"CNN fear_and_greed = {parsed}"
    results.append(fng)

    # Indices (TAIEX/SPX/KLCI).
    idx_state: dict[str, object] = {}

    def fetch_idx() -> object:
        hits: dict[str, float] = {}
        misses: list[str] = []
        for sym in sentiment_src.INDEX_SYMBOLS:
            c = sentiment_src.fetch_yf_close(sym)
            if c is not None:
                hits[sym] = c
            else:
                misses.append(sym)
        idx_state["hits"] = hits
        idx_state["misses"] = misses
        return hits

    idx = run_probe("yfinance (index)", DataType.QUOTE_LATEST, "INDEX", fetch_idx,
                    requires_key=False)
    hits = idx_state.get("hits", {})
    idx.coverage_hits = len(hits)
    idx.coverage_misses = idx_state.get("misses", [])
    idx.batch_max = len(sentiment_src.INDEX_SYMBOLS)
    if hits:
        idx.sample_value = Decimal(str(next(iter(hits.values()))))
    idx.notes = f"TAIEX/SPX/KLCI closes = {hits}"
    results.append(idx)


def _skipped(source: str, data_type: DataType, market: str, notes: str) -> ProbeResult:
    return ProbeResult(
        source=source, data_type=data_type, market=market,
        requires_key=True, verdict=Verdict.SKIPPED, notes=notes,
    )


def _probe_keyed_skips(results: list[ProbeResult]) -> None:
    no_key = "no key supplied this round"
    fm_token = finmind_src.finmind_token()
    if fm_token is None:
        results.append(_skipped("finmind", DataType.QUOTE_LATEST, "TW", no_key))
        results.append(_skipped("finmind", DataType.DIVIDEND, "TW", no_key))
        results.append(_skipped("finmind", DataType.FX, "FX", no_key))
        # spec-20.6 chips datasets ride the same token; skipped together when absent.
        for ds in finmind_src.FINMIND_DATASETS:
            results.append(
                _skipped("finmind", DataType.QUOTE_LATEST, "TW", f"{no_key} ({ds})")
            )
        # spec-20.15.5 quota/tier note: needs the token; skipped together when absent.
        results.append(_skipped("finmind", DataType.QUOTE_LATEST, "TW", f"{no_key} (quota)"))
    else:
        # spec-20.15.5: with a key, report usage/limit + inferred tier (Bearer auth).
        try:
            quota = finmind_src.fetch_quota(fm_token)
            limit = quota.get("api_request_limit")
            tier = finmind_src.tier_from_limit(limit) or "unknown"
            note = f"used {quota.get('user_count')}/{limit}; tier={tier}"
            verdict = Verdict.FALLBACK
        except Exception as exc:  # noqa: BLE001 - a quota probe failure is a valid result
            note, verdict = f"quota probe failed: {exc}", Verdict.UNUSABLE
        results.append(ProbeResult(
            source="finmind", data_type=DataType.QUOTE_LATEST, market="TW",
            requires_key=True, verdict=verdict, notes=note,
        ))
    if us_alt.alpha_key() is None:
        results.append(_skipped("alphavantage", DataType.QUOTE_LATEST, "US", no_key))
    if us_alt.finnhub_key() is None:
        results.append(_skipped("finnhub", DataType.QUOTE_LATEST, "US", no_key))
    # spec-20.9 pending token sources catalogued but not validated online this round.
    results.append(_skipped("fred", DataType.QUOTE_LATEST, "FX", f"{no_key} (macro)"))
    results.append(_skipped("schwab", DataType.QUOTE_LATEST, "US", f"{no_key} (OAuth)"))


def _assign_primary_fallback(results: list[ProbeResult]) -> None:
    """Light judgment pass: pick PRIMARY among working (FALLBACK-returned) candidates.

    ``run_probe`` returns FALLBACK on any successful call (it has no notion of
    PRIMARY); here we promote the strongest no-key source per (data_type, market)
    to PRIMARY and leave the rest as FALLBACK / UNUSABLE / SKIPPED as measured.
    """
    primary_picks = {
        (DataType.QUOTE_LATEST, "US"): "yfinance",
        (DataType.QUOTE_LATEST, "TW"): "tw_gov (TWSE)",
        (DataType.QUOTE_LATEST, "MY"): "yfinance",
        (DataType.QUOTE_HISTORY, "US"): "yfinance",
        (DataType.QUOTE_HISTORY, "TW"): "yfinance",
        (DataType.QUOTE_HISTORY, "MY"): "yfinance",
        (DataType.FX, "FX"): "yfinance",
        (DataType.DIVIDEND, "US"): "yfinance",
    }
    for result in results:
        if result.verdict is Verdict.FALLBACK:
            key = (result.data_type, result.market)
            if primary_picks.get(key) == result.source:
                result.verdict = Verdict.PRIMARY


def main() -> None:
    results: list[ProbeResult] = []

    # --- yfinance: latest quotes (one batch download per market) ---
    us_codes = yfinance_src.US
    _probe_yfinance_quote_latest(results, "US", us_codes, us_codes)

    tw_codes = yfinance_src.TW
    tw_symbols = [_yf_suffixed_tw(c) for c in tw_codes]
    _probe_yfinance_quote_latest(results, "TW", tw_codes, tw_symbols)

    my_codes = yfinance_src.MY
    my_symbols = [f"{c}{_MY_SUFFIX}" for c in my_codes]
    _probe_yfinance_quote_latest(results, "MY", my_codes, my_symbols)

    # --- yfinance: history (one representative ticker per market) ---
    _probe_yfinance_history(results, "US", "AAPL")
    _probe_yfinance_history(results, "TW", "2330.TW")
    _probe_yfinance_history(results, "MY", "3182.KL")

    # --- yfinance: FX (3 pairs, one batch) ---
    _probe_yfinance_fx(results)

    # --- yfinance: dividends (one representative) ---
    _probe_yfinance_dividend(results, "AAPL")

    # --- tw_gov: TWSE listed + TPEx OTC ---
    _probe_tw_gov_listed(results)
    _probe_tw_gov_otc(results)

    # --- twstock: realtime ---
    _probe_twstock(results)

    # --- us_alt: stockprices.dev (no-key fallback) ---
    _probe_us_alt_stockprices(results)

    # --- my_src: klsescreener + malaysiastock (3-dp string corroboration) ---
    _probe_my_klsescreener(results)
    _probe_my_malaysiastock(results)

    # --- sentiment + index reachability (VIX / CNN F&G / TAIEX·SPX·KLCI) ---
    _probe_sentiment_index(results)

    # --- keyed sources without keys: SKIPPED, never called ---
    _probe_keyed_skips(results)

    _assign_primary_fallback(results)

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(render_report(results), encoding="utf-8")
    print(f"wrote {REPORT} with {len(results)} results")


if __name__ == "__main__":
    main()
