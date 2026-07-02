"""Data-source management API (spec 14): keys, health, per-MARKET quote order.

Thin over ``pricing.datasources_store``: it reads/writes the data_sources tables
and serializes the masked view. It computes no money and no returns; the
``/test`` endpoint performs a single, time-bounded provider probe (run off the event
loop) and records the result into ``data_source_health``. The per-market quote
order (2026-07-03, item 9) is the REAL chain ``default_registry`` walks.
"""

import sqlite3
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.errors import error_body
from portfolio_dash.pricing import datasources_store as store
from portfolio_dash.pricing.defaults import default_registry
from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing import sentiment_source
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.providers.finmind_provider import FinMindProvider
from portfolio_dash.pricing.providers.klsescreener_provider import KlseScreenerProvider
from portfolio_dash.pricing.providers.malaysiastock_provider import MalaysiaStockProvider
from portfolio_dash.pricing.providers.stockprices_dev_provider import StockPricesDevProvider
from portfolio_dash.pricing.providers.tpex_provider import TpexProvider
from portfolio_dash.pricing.providers.twse_provider import TwseProvider
from portfolio_dash.pricing.providers.twstock_provider import TwStockProvider
from portfolio_dash.pricing.providers.yfinance_provider import YFinanceProvider
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.shared.enums import Market

router = APIRouter()

_PROBE_TIMEOUT_S = 10.0


# --- GET /api/datasources -----------------------------------------------------


def _source_wire(
    info: store.SourceInfo, state: store.SourceState | None
) -> dict[str, Any]:
    """Merge a static source description with its persisted (masked) state.

    ``provides``/``status`` carry the spec-20.1 catalog. A catalog ``status`` of
    ``pending``/``blocked`` (not yet validated / unusable) wins over the dynamic
    health status; a ``live`` source's wire ``status`` follows its health row.
    """
    api_key = state.api_key if state is not None else None
    token_masked = store.mask_token(api_key)
    # auth:"none" sources have no key; their status follows health, defaulting "ok".
    if state is None:
        status = "off" if info.auth == "apikey" else "unknown"
        last_test: str | None = None
        latency_ms: int | None = None
    else:
        status = state.status
        last_test = state.last_test
        latency_ms = state.latency_ms
        # An apikey source with no key set surfaces as "off" (spec 14.1).
        if info.auth == "apikey" and not api_key and status == "unknown":
            status = "off"
    # Catalog readiness overrides the dynamic health status for non-live sources.
    if info.status != "live":
        status = info.status
    return {
        "id": info.id,
        "name": info.name,
        "type": info.type,
        "markets": info.markets,
        "auth": info.auth,
        "provides": info.provides,
        "token_masked": token_masked,
        "status": status,
        "last_test": last_test,
        "latency_ms": latency_ms,
        "tier": state.tier if state is not None else None,  # current marking (spec 20.15.2)
        "tiers": info.tiers,  # selectable options for the panel dropdown
        "note": info.note,
    }


@router.get("/datasources")
def list_datasources(
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    store.ensure_seeded(conn)
    states = store.list_states(conn)
    sources = [_source_wire(info, states.get(info.id)) for info in store.SOURCE_INFO]
    # Per-MARKET quote order (2026-07-03, item 9 — supersedes the per-account
    # fallback wire): `market_order` is the REAL fetch chain default_registry
    # uses; `market_order_available` lists every provider capable of quoting
    # that market (the editor's pick list).
    registry = default_registry(conn)
    return {
        "sources": sources,
        "market_order": {m.value: chain for m, chain in store.quote_order(conn).items()},
        "market_order_available": {
            m.value: registry.capable_ids(DataType.QUOTE_LATEST, m) for m in Market
        },
    }


# --- PUT /api/datasources/{id}/key --------------------------------------------


class KeyBody(BaseModel):
    api_key: str  # empty string clears the key


@router.put("/datasources/{source_id}/key")
def set_key(
    source_id: str,
    body: KeyBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    store.ensure_seeded(conn)
    info = store.SOURCE_INFO_BY_ID.get(source_id)
    if info is None:
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", f"未知資料來源：{source_id}"),
        )
    if info.auth == "none":
        return JSONResponse(
            status_code=400,
            content=error_body(
                "validation_error", f"{info.name} 無需金鑰", field="api_key"
            ),
        )
    store.set_api_key(conn, source_id, body.api_key)
    return {
        "id": source_id,
        "token_masked": store.mask_token(body.api_key or None),
        "status": "unknown",
    }


# --- PUT /api/datasources/{id}/tier -------------------------------------------


class TierBody(BaseModel):
    tier: str | None  # null clears the marking; must be one of the source's `tiers`


@router.put("/datasources/{source_id}/tier")
def set_tier(
    source_id: str,
    body: TierBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    """Mark a source's token tier (spec 20.15.3). 404 unknown id; 400 if the source has
    no selectable tiers (auth:"none") or the value is not one of its options."""
    store.ensure_seeded(conn)
    info = store.SOURCE_INFO_BY_ID.get(source_id)
    if info is None:
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", f"未知資料來源：{source_id}"),
        )
    if not info.tiers:
        return JSONResponse(
            status_code=400,
            content=error_body(
                "validation_error", f"{info.name} 無資費等級", field="tier"
            ),
        )
    if body.tier is not None and body.tier not in info.tiers:
        return JSONResponse(
            status_code=400,
            content=error_body(
                "validation_error", f"未知資費等級：{body.tier}", field="tier"
            ),
        )
    store.set_tier(conn, source_id, body.tier)
    return {"id": source_id, "tier": body.tier}


# --- POST /api/datasources/{id}/test ------------------------------------------


# Free-source probe samples: a single minimal request per source (spec 20.11).
_PROBE_TW = InstrumentRef(symbol="2330", market=Market.TW)  # TWSE-listed (twse/yfinance/finmind)
_PROBE_TPEX = InstrumentRef(symbol="5347", market=Market.TW)  # a TPEx OTC counter (2330 is TWSE)
_PROBE_US = InstrumentRef(symbol="AAPL", market=Market.US)
_PROBE_MY = InstrumentRef(symbol="5212", market=Market.MY)


def _probe_quote_provider(
    provider: ProviderBase, ref: InstrumentRef
) -> tuple[bool, str | None]:
    """Run a single ``fetch_quote_latest`` and report ok/empty (raising falls through)."""
    rows = provider.fetch_quote_latest([ref])
    if rows:
        return True, f"{ref.symbol} = {rows[0].close}"
    return False, f"{ref.symbol} 無回應"


def _probe_free_source(source_id: str) -> tuple[bool, str | None] | None:
    """Probe a wired free (key-less) source; None when this id has no wired probe."""
    if source_id == "yfinance":
        return _probe_quote_provider(YFinanceProvider(), _PROBE_US)
    if source_id == "twse":
        return _probe_quote_provider(TwseProvider(), _PROBE_TW)
    if source_id == "tpex":
        return _probe_quote_provider(TpexProvider(), _PROBE_TPEX)
    if source_id == "twstock":
        return _probe_quote_provider(TwStockProvider(), _PROBE_TW)
    if source_id == "stockprices_dev":
        return _probe_quote_provider(StockPricesDevProvider(), _PROBE_US)
    if source_id == "klsescreener":
        return _probe_quote_provider(KlseScreenerProvider(), _PROBE_MY)
    if source_id == "malaysiastock":
        return _probe_quote_provider(MalaysiaStockProvider(), _PROBE_MY)
    if source_id == "cnn_fng":
        fng = sentiment_source.fetch_fear_greed()
        return (fng is not None, f"score={fng['score']}" if fng else "CNN 無回應")
    return None


def probe_source(source_id: str, api_key: str | None) -> tuple[bool, str | None]:
    """Production connection test for a source: True/False + an optional detail.

    Real provider call (a single minimal request); raising or returning False is a
    valid "error" test result. Tests monkeypatch this (or the providers) so no
    network I/O occurs. Catalog-only / token-gated sources report a neutral result.
    """
    info = store.SOURCE_INFO_BY_ID.get(source_id)
    if info is None:
        return False, "未知資料來源"
    if info.status == "blocked":
        return False, "來源受阻（catalogue only）"
    if info.auth in ("apikey", "oauth") and not api_key:
        return False, "尚未設定金鑰"
    if info.status == "pending":
        # Token-gated adapter catalogued but not validated online this round (spec 20.9).
        return False, "待測試（尚未線上驗證）"
    if source_id == "finmind":
        # Keyed live source: one minimal FinMind dividend request (spec 14 / 20.11). A raised
        # HTTPError (bad/expired key, over quota) becomes an "error" result in ``_run_probe``.
        events = FinMindProvider(token=api_key).fetch_dividends([_PROBE_TW])
        return True, f"{len(events)} 筆股利回應"
    free = _probe_free_source(source_id)
    if free is not None:
        return free
    # Live sources without a wired probe yet report a neutral non-network result.
    return False, "尚未實作連線測試"


def _run_probe(source_id: str, api_key: str | None) -> tuple[str, int | None, str | None]:
    """Run ``probe_source`` with latency timing; map exceptions to an error result."""
    start = time.monotonic()
    try:
        ok, detail = probe_source(source_id, api_key)
    except Exception as exc:  # noqa: BLE001 - any probe failure is a valid "error" result
        return "error", None, str(exc) or exc.__class__.__name__
    latency_ms = int((time.monotonic() - start) * 1000)
    if ok:
        return "ok", latency_ms, detail
    return "error", None, detail


@router.post("/datasources/{source_id}/test")
async def test_source(
    source_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
    now: datetime = Depends(get_now),
) -> Any:
    store.ensure_seeded(conn)
    if source_id not in store.KNOWN_SOURCE_IDS:
        return JSONResponse(
            status_code=404,
            content=error_body("not_found", f"未知資料來源：{source_id}"),
        )
    api_key = store.get_api_key(conn, source_id)
    status, latency_ms, detail = await run_in_threadpool(_run_probe, source_id, api_key)
    last_test = now.isoformat()
    store.upsert_health(
        conn,
        source_id,
        status=status,
        last_test=last_test,
        latency_ms=latency_ms,
        detail=detail,
    )
    return {
        "id": source_id,
        "status": status,
        "latency_ms": latency_ms,
        "detail": detail,
        "last_test": last_test,
    }


# --- PUT /api/datasources/market-order ------------------------------------------
# Supersedes PUT /datasources/fallbacks (2026-07-03, item 9): quote routing is a
# property of the MARKET, and the stored order is consumed by default_registry —
# what you see is what the fetcher does.


class MarketOrderBody(BaseModel):
    market: Market
    order: list[str]


@router.put("/datasources/market-order")
def set_market_order(
    body: MarketOrderBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Any:
    store.ensure_seeded(conn)
    if not body.order:
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", f"{body.market.value} 的抓取順位不可為空", field="order"))
    if len(set(body.order)) != len(body.order):
        return JSONResponse(status_code=400, content=error_body(
            "validation_error", "順位清單含重複來源", field="order"))
    capable = set(default_registry(conn).capable_ids(DataType.QUOTE_LATEST, body.market))
    for src in body.order:
        if src not in capable:
            return JSONResponse(status_code=400, content=error_body(
                "validation_error",
                f"{src} 不支援 {body.market.value} 市場報價", field="order"))
    store.set_quote_order(conn, body.market, body.order)
    return {
        "market_order": {m.value: chain for m, chain in store.quote_order(conn).items()}
    }
