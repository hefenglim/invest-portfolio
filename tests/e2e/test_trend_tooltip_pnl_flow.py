"""E2E: the trend tooltip's 累計總損益（含匯兌） is the SERVER's ``pnl`` (owner rulings 2026-10-22).

The tooltip used to print ``pt.total_value - pt.net_invested`` subtracted in JS floats and
coloured by the float's sign. Now each trend point carries ``pnl`` — that difference at the
reporting currency's minor unit, computed by the server, ``null`` on an incomplete day — and the
tooltip only formats it, in the series' own reporting currency.

**TWD ledger** (one share bought at 500 six days ago; a second symbol bought three days later,
priced only from the next day), a real mouse hover over four days:

- a gain that rounds AWAY (500.3 − 500 = 0.3): the server sends ``"0"``, so the tooltip reads
  ``0`` in the neutral colour. The float path printed ``+0`` in the gain colour — this day
  fails on the code before the fix;
- a half-up tie (512.5 − 500 = 12.5): ``+13``, gain colour;
- a loss (488.7 − 500 = −11.3): ``−11`` (U+2212), loss colour;
- an INCOMPLETE day (the second symbol has no price yet): ``null`` → 「—」 in the neutral
  colour, while 總市值 and 累計淨投入 still show and the 「部分標的當日無價格」 note stays.

**USD-reporting ledger**: one US share bought at 100, closing at 112.497. The server sends
``"12.50"``; formatted in USD the line reads ``+12.50``. The tooltip used a hard-coded 'TWD'
(0 dp) until the same day: the float path printed ``+12`` and the first server-``pnl`` version
``+13`` (the 2-dp value rounded a second time) — 總市值 ``112`` instead of ``112.50``.

Each line is checked against the literal text and against ``fmt`` of the payload's own value.
The colour is read with ``getComputedStyle`` (DEF-077: the colour a user sees), against the
theme's ``--up`` / ``--down`` / ``--text-2``. The dates are relative to today (the flow server
runs on the real clock), so every day is inside the default 全部 range.
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
_LATE = _BUY + timedelta(days=3)          # CCC bought, priced only from the next day
_EM_DASH = "—"
#: day → (總市值 shown, the 累計總損益 text, the colour token it must be painted in)
_TWD_DAYS: dict[date, tuple[str, str, str]] = {
    _BUY: ("500.3", "0", "--text-2"),
    _BUY + timedelta(days=1): ("512.5", "+13", "--up"),
    _BUY + timedelta(days=2): ("488.7", "−11", "--down"),
    _LATE: ("488.7", _EM_DASH, "--text-2"),     # incomplete: CCC has no price yet
}


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Iterator[None]:
    """Re-enable loopback sockets per test (matches the other flow modules)."""
    enable_socket()
    socket_allow_hosts(["127.0.0.1", "localhost"], allow_unix_socket=True)
    yield
    disable_socket(allow_unix_socket=True)


def _stamp(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), _TZ)


def _seed_twd(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    for sym in ("BBB", "CCC"):
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Semis",
                                           name=f"{sym} Corp", board="TWSE"))
    for sym, price, day in (("BBB", "500", _BUY), ("CCC", "100", _LATE)):
        insert_transaction(conn, account_id="tw_broker", symbol=sym, side=Side.BUY,
                           quantity=Decimal("1"), price=Decimal(price),
                           fees=Decimal("0"), tax=Decimal("0"), trade_date=day)
    upsert_prices(conn, [
        PriceRow(instrument="BBB", market=Market.TW, as_of=day, close=Decimal(close),
                 source="test")
        for day, (close, _text, _colour) in list(_TWD_DAYS.items())[:3]
    ] + [PriceRow(instrument="CCC", market=Market.TW, as_of=_LATE + timedelta(days=1),
                  close=Decimal("100"), source="test")],
        fetched_at=_stamp(_LATE + timedelta(days=1)))


def _seed_usd(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="AAA", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="AAA Inc"))
    insert_transaction(conn, account_id="schwab", symbol="AAA", side=Side.BUY,
                       quantity=Decimal("1"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=_BUY)
    upsert_prices(conn, [PriceRow(instrument="AAA", market=Market.US, as_of=_BUY,
                                  close=Decimal("112.497"), source="test")],
                  fetched_at=_stamp(_BUY))


#: The tooltip of `day` (after a hover): its lines by label, the 累計總損益 value's computed
#: colour, and the computed colours of the three tokens.
_READ_TOOLTIP = """
(day) => {
  const host = document.getElementById('trend-chart');
  const box = [...host.querySelectorAll('div')].find(
    (d) => d.textContent.indexOf(day) === 0 && d.textContent.indexOf('累計總損益（含匯兌）') > 0
           && [...d.children].some((c) => c.textContent === day));
  if (!box) return null;
  const lines = {};
  let pnlColor = null;
  for (const line of box.children) {
    const b = line.querySelector('b');
    const label = b ? line.textContent.slice(0, line.textContent.indexOf(b.textContent)).trim()
                    : line.textContent.trim();
    lines[label] = b ? b.textContent : '';
    if (label === '累計總損益（含匯兌）') pnlColor = getComputedStyle(b).color;
  }
  const paint = (token) => {
    const probe = document.createElement('span');
    probe.style.color = 'var(' + token + ')';
    document.body.appendChild(probe);
    const c = getComputedStyle(probe).color;
    probe.remove();
    return c;
  };
  return { lines, pnlColor,
           tokens: { '--up': paint('--up'), '--down': paint('--down'),
                     '--text-2': paint('--text-2') } };
}
"""


def _open(page: Page, base_url: str) -> dict[str, Any]:
    page.goto(base_url + "/index.html", wait_until="load")
    page.wait_for_selector(".kpi-card")
    page.wait_for_function("() => document.querySelector('#trend-chart canvas') !== null")
    payload: dict[str, Any] = page.evaluate(
        "() => fetch('/api/dashboard').then((r) => r.json())")
    return payload


def _hover(page: Page, day: date, value: str) -> dict[str, Any]:
    """Move the real mouse onto the 總市值 point of *day*; return the tooltip it opens."""
    xy = page.evaluate(
        "([day, value]) => { const el = document.getElementById('trend-chart');"
        " const inst = window.echarts.getInstanceByDom(el);"
        " const [x, y] = inst.convertToPixel({xAxisIndex: 0, yAxisIndex: 0}, [day, value]);"
        " const r = el.getBoundingClientRect(); return [r.left + x, r.top + y]; }",
        [day.isoformat(), float(value)])
    page.mouse.move(xy[0], xy[1] - 30)
    page.mouse.move(xy[0], xy[1])
    page.wait_for_function(f"({_READ_TOOLTIP})({day.isoformat()!r}) !== null")
    got: dict[str, Any] = page.evaluate(f"({_READ_TOOLTIP})({day.isoformat()!r})")
    return got


def _fmt(page: Page, fn: str, value: Any, ccy: str) -> str:
    text: str = page.evaluate(f"([v, c]) => window.fmt.{fn}(v, c)", [value, ccy])
    return text


@pytest.mark.e2e
def test_the_trend_tooltip_shows_the_servers_pnl(
    flow_server: FlowServerFactory, fresh_page: Page
) -> None:
    base_url = flow_server(_seed_twd)
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    payload = _open(page, base_url)
    assert payload["trend"]["reporting_currency"] == "TWD"
    points = {p["date"]: p for p in payload["trend"]["points"]}

    for day, (value, text, colour) in _TWD_DAYS.items():
        point = points[day.isoformat()]
        assert point["total_value"] == value, point   # the seed landed as designed
        assert point["incomplete"] is (day == _LATE), point
        got = _hover(page, day, value)
        lines = got["lines"]
        assert lines["累計總損益（含匯兌）"] == text == _fmt(page, "signed", point["pnl"], "TWD"), \
            (day, got, point)
        assert got["pnlColor"] == got["tokens"][colour], (day, colour, got)
        # the other lines still show, and the note keeps marking an incomplete day
        assert lines["總市值"] == _fmt(page, "money", point["total_value"], "TWD"), got
        assert lines["累計淨投入"] == _fmt(page, "money", point["net_invested"], "TWD"), got
        assert ("部分標的當日無價格" in lines) is (day == _LATE), (day, lines)
    assert points[_LATE.isoformat()]["pnl"] is None
    assert not page_errors, page_errors


@pytest.mark.e2e
def test_a_usd_reporting_tooltip_formats_in_usd(
    flow_server: FlowServerFactory, fresh_page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The series' own currency formats the card: cents shown, and the server's 2-dp ``pnl``
    is never rounded again (12.497 → ``12.50`` → ``+12.50``, not ``+13``)."""
    monkeypatch.setenv("REPORTING_CURRENCY", "USD")   # read by the flow server's settings
    base_url = flow_server(_seed_usd)
    page = fresh_page
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    payload = _open(page, base_url)
    assert payload["trend"]["reporting_currency"] == "USD"
    point = next(p for p in payload["trend"]["points"] if p["date"] == _BUY.isoformat())
    assert (Decimal(point["total_value"]), Decimal(point["net_invested"]), point["pnl"]) == (
        Decimal("112.497"), Decimal("100"), "12.50"), point

    got = _hover(page, _BUY, point["total_value"])
    lines = got["lines"]
    assert lines["累計總損益（含匯兌）"] == "+12.50" == _fmt(page, "signed", point["pnl"], "USD")
    assert got["pnlColor"] == got["tokens"]["--up"], got
    assert lines["總市值"] == "112.50", lines
    assert lines["累計淨投入"] == "100.00", lines
    assert lines["總淨值（含現金）"] == _fmt(page, "money", point["net_worth"], "USD"), lines
    sub = page.evaluate("() => document.getElementById('trend-sub').textContent")
    assert sub == "USD・日線", sub
    nw = page.evaluate("() => document.getElementById('trend-networth').textContent")
    assert nw == "含現金 " + _fmt(page, "money", payload["trend"]["points"][-1]["net_worth"],
                                  "USD"), nw
    assert "." in nw, nw   # cents shown
    assert not page_errors, page_errors
