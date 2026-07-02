from fastapi import APIRouter

from portfolio_dash.shared.buildinfo import get_build_info

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe + build identity (version / short commit / release-tag status).

    Open (no auth) so it doubles as the post-deploy identity check:
    ``curl -s http://127.0.0.1:8400/api/health`` ->
    ``{"status":"ok","version":"0.1.4","commit":"abc1234","release":"v0.1.4"}``.
    ``release`` is the exact tag on HEAD (a promoted release), else ``"unreleased"``
    — so one call tells version, commit, and whether this is released code.
    """
    info = get_build_info()
    return {
        "status": "ok",
        "version": info.version,
        "commit": info.commit,
        "release": info.release,
    }
