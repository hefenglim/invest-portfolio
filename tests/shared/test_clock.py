"""shared.clock.app_now — the single business day-anchor clock (M1 fix, decision Q6)."""

from datetime import timedelta
from zoneinfo import ZoneInfo

from portfolio_dash.shared.clock import app_now
from portfolio_dash.shared.config import get_settings


def test_app_now_is_tz_aware_in_display_tz() -> None:
    now = app_now()
    assert now.tzinfo is not None
    expected = ZoneInfo(get_settings().tz_display)
    assert now.utcoffset() == now.astimezone(expected).utcoffset()


def test_app_now_default_is_taipei() -> None:
    # The default tz_display is Asia/Taipei (+08:00, no DST).
    assert app_now().utcoffset() == timedelta(hours=8)
