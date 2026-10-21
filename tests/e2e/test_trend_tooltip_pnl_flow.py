"""E2E: the trend tooltip's 累計總損益（含匯兌） is the SERVER's ``pnl`` (owner ruling 2026-10-22).

The tooltip used to print ``pt.total_value - pt.net_invested`` subtracted in JS floats and
coloured by the float's sign. Now each trend point carries ``pnl`` — that difference at the
reporting currency's minor unit, computed by the server — and the tooltip only formats it.

A real mouse hover over three days of a small TWD ledger (one share bought at 500 six days ago):

- a gain that rounds AWAY (500.3 − 500 = 0.3): the server sends ``"0"``, so the tooltip reads
  ``0`` in the neutral colour. The float path printed ``+0`` in the gain colour — this day
  fails on the old code;
- a half-up tie (512.5 − 500 = 12.5): ``+13``, gain colour;
- a loss (488.7 − 500 = −11.3): ``−11`` (U+2212), loss colour.

Each line is checked twice: against the literal text, and against ``fmt.signed`` of the
payload's own ``pnl`` (so the page shows the value the server sent). The colour is read with
``getComputedStyle`` (DEF-077: the colour a user sees), against the theme's ``--up`` /
``--down`` / ``--text-2``.

The dates are relative to today (the flow server runs on the real clock), so every day is
inside the default 全部 range.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Page
from pytest_socket import disable_socket, enable_socket, socket_allow_hosts

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.e2e.conftest import FlowServerFactory

_TZ = ZoneInfo("Asia/Taipei")
_TODAY = datetime.now(_TZ).date()
_BUY = _TODAY - timedelta(days=6)
#: day → (close, the tooltip's expected text, the colour token it must be painted in)
_DAYS: dict[date, tuple[str, str, str]] = {
    _BUY: ("500.3", "0", "--text-2"),
    _BUY + timedelta(days=1): ("512.5", "+13", "--up"),
    _BUY + timedelta(days=2): ("488.7", "−11", "--down"),
}


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets per test (matches the other flow modules)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="BBB", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semis", name="BBB Corp", board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="BBB", side=Side.BUY,
                       quantity=Decimal("1"), price=Decimal("500"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=_BUY)
    upsert_prices(conn, [
        PriceRow(instrument="BBB", market=Market.TW, as_of=day, close=Decimal(close),
                 source="test")
        for day, (close, _text, _colour) in _DAYS.items()
    ], fetched_at=datetime.combine(_BUY + timedelta(days=2), datetime.min.time(), _TZ))


#: Hover the 總市值 point of `day` with the real mouse, then return the tooltip's
#: 累計總損益（含匯兌） line: its text, its computed colour, and the colours of the three tokens.
_READ_TOOLTIP = """
(day) => {
  const host = document.getElementById('trend-chart');
  const line = [...host.querySelectorAll('div')].find(
    (d) => d.children.length === 1 && d.textContent.indexOf('累計總損益（含匯兌）') === 0
           && d.parentElement && d.parentElement.textContent.indexOf(day) === 0);
  if (!line) return null;
  const b = line.querySelector('b');
  const paint = (token) => {
    const probe = document.createElement('span');
    probe.style.color = 'var(' + token + ')';
    document.body.appendChild(probe);
    const c = getComputedStyle(probe).color;
    probe.remove();
    return c;
  };
  return { text: b.textContent, color: getComputedStyle(b).color,
           tokens: { '--up': paint('--up'), '--down': paint('--down'),
                     '--text-2': paint('--text-2') } };
}
"""


@pytest.mark.e2e
def test_the_trend_tooltip_shows_the_servers_pnl(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base_url = flow_server(_seed)
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    page.goto(base_url + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")
    page.wait_for_function("() => document.querySelector('#trend-chart canvas') !== null")
    payload: dict[str, Any] = page.evaluate(
        "() => fetch('/api/dashboard').then((r) => r.json())")
    points = {p["date"]: p for p in payload["trend"]["points"]}

    for day, (close, text, colour) in _DAYS.items():
        point = points[day.isoformat()]
        assert point["total_value"] == close, point   # the seed landed as designed
        xy = page.evaluate(
            "([day, value]) => { const el = document.getElementById('trend-chart');"
            " const inst = window.echarts.getInstanceByDom(el);"
            " const [x, y] = inst.convertToPixel({xAxisIndex: 0, yAxisIndex: 0}, [day, value]);"
            " const r = el.getBoundingClientRect(); return [r.left + x, r.top + y]; }",
            [day.isoformat(), float(close)])
        page.mouse.move(xy[0], xy[1] - 30)
        page.mouse.move(xy[0], xy[1])
        page.wait_for_function(f"({_READ_TOOLTIP})({day.isoformat()!r}) !== null")
        got = page.evaluate(f"({_READ_TOOLTIP})({day.isoformat()!r})")
        server = page.evaluate("(v) => window.fmt.signed(v, 'TWD')", point["pnl"])
        assert got["text"] == text == server, (day, got, point)
        assert got["color"] == got["tokens"][colour], (day, colour, got)

    assert not page_errors, page_errors
