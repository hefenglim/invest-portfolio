from fastapi import APIRouter

from portfolio_dash import __version__

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe + app version (single-source ``portfolio_dash.__version__``).

    Open (no auth) so it doubles as a quick post-deploy version check:
    ``curl -s http://127.0.0.1:8400/api/health`` -> ``{"status":"ok","version":"…"}``.
    """
    return {"status": "ok", "version": __version__}
