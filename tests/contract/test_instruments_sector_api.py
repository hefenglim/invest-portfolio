"""Contract: GET /api/instruments/sectors (FU-D31 canonical sector vocabulary).

The former POST /api/instruments/ai-sector endpoint was MERGED into the unified
POST /api/instruments/ai-resolve (R6-B) — its behaviours (canonical mapping, off-vocabulary
downgrade, degradation envelope) are exercised in ``test_instruments_ai_resolve.py``. This
file now guards only the sector-vocabulary read that seeds the dropdowns.
"""

from fastapi.testclient import TestClient

from portfolio_dash.shared.sectors import CANONICAL_SECTORS


def test_sectors_endpoint_returns_canonical_vocabulary(api_client: TestClient) -> None:
    r = api_client.get("/api/instruments/sectors")
    assert r.status_code == 200
    sectors = r.json()["sectors"]
    assert len(sectors) == len(CANONICAL_SECTORS)
    keys = [s["key"] for s in sectors]
    assert keys == [s["key"] for s in CANONICAL_SECTORS]  # order preserved
    # R6 GICS vocabulary: Information Technology leads, ETF is the non-GICS bucket, and the
    # folded-away FU-D31 keys are gone; Unclassified is always last.
    assert "Information Technology" in keys and "ETF" in keys
    assert "Semiconductors" not in keys and "Shipping" not in keys
    assert keys[-1] == "Unclassified"
    for s in sectors:
        assert s["key"] and s["zh"]  # dual-text label material
