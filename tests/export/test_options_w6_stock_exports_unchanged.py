"""Options W6 — every export of a STOCK ledger is byte-identical to before (spec §10, inv. 1).

The digests below were recorded by running :func:`export_digests` against the code at
``7f23c51`` (the merged wave-1 tip, before W6 touched an exporter) — so this is a real
"before / after" comparison, not a re-statement of the current output. The one allowed
difference is spec §10's additive change (3): the realized CSV appends ``asset_type`` and
``contract``. That export is compared after stripping exactly those two trailing columns, and
the test asserts the stripped cells are ``stock`` and empty on every row.

The ledger is the golden one plus a TW and a US sale (realized rows in both currencies), a
schwab deposit (a cash-statement movement line) and a future-dated buy (DEF-056 badges), so
every section an option branch now sits beside is exercised. The printed reports carry the
package version; it is pinned to a constant so a release does not read as an export change.

A digest that changes because an export changed ON PURPOSE is re-recorded in the same commit
(spec §10: "re-pinned in the same commit wherever a test pins it") — never to make a stock
ledger's export move as a side effect of the options work.
"""

from __future__ import annotations

import hashlib
import io
import sqlite3
import zipfile
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.store import insert_cash_movement, insert_transaction
from portfolio_dash.export import report_html
from portfolio_dash.export.cash_statement import (
    build_cash_statement_csv,
    build_cash_statement_report_html,
)
from portfolio_dash.export.holdings import build_holdings_csv
from portfolio_dash.export.holdings_report import build_holdings_report_html
from portfolio_dash.export.ledgers_report import build_ledgers_report_html
from portfolio_dash.export.realized import build_realized_csv
from portfolio_dash.export.rebalance_report import build_rebalance_report_html
from portfolio_dash.export.symbol_detail import build_symbol_detail_csv
from portfolio_dash.export.tax import build_tax_package_zip
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, _seed_golden, init_golden_base

D = Decimal
_PINNED_VERSION = "0.0.0-test"


def stock_ledger() -> sqlite3.Connection:
    """The golden ledger + two sales, a deposit and a future-dated buy — no option anywhere."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    _seed_golden(conn)
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.SELL,
                       quantity=D("100"), price=D("600"), fees=D("85"), tax=D("180"),
                       trade_date=date(2026, 6, 10))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.SELL,
                       quantity=D("2"), price=D("110.5"), fees=D("0.01"), tax=D("0"),
                       trade_date=date(2026, 5, 4))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=D("10"), price=D("610"), fees=D("20"), tax=D("0"),
                       trade_date=date(2026, 7, 1))
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 1, 9),
                         kind="DEPOSIT", ccy=Currency.USD, amount=D("50"), note="seed")
    conn.commit()
    return conn


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _zip_sha(content: bytes) -> str:
    """A zip's MEMBERS, not its bytes — ``ZipFile.writestr`` stamps the wall clock."""
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        parts = [f"{n}:{_sha(zf.read(n))}" for n in sorted(zf.namelist())]
    return _sha("\n".join(parts).encode())


def strip_realized_additions(content: bytes) -> bytes:
    """The realized CSV with spec §10 (3)'s two appended columns removed again, asserting
    they read ``asset_type,contract`` on the header and ``stock,`` (empty contract) on every
    row of a stock ledger. A file that does not carry them is returned unchanged, so the
    function also reads the BEFORE bytes."""
    text = content.decode("utf-8")
    lines = text.split("\r\n")
    if not lines[0].endswith(",asset_type,contract"):
        return content
    out: list[str] = []
    for i, ln in enumerate(lines):
        if i == 0:
            out.append(ln[: -len(",asset_type,contract")])
        elif ln and not ln.startswith("#"):
            assert ln.endswith(",stock,"), ln
            out.append(ln[: -len(",stock,")])
        else:
            out.append(ln)
    return "\r\n".join(out).encode("utf-8")


def export_digests(conn: sqlite3.Connection) -> dict[str, str]:
    """Every W6-touched export of *conn*, as a digest per (export, variant)."""
    now = GOLDEN_NOW
    twd = Currency.TWD
    out: dict[str, str] = {}
    out["holdings_csv"] = _sha(build_holdings_csv(conn, now=now, reporting=twd).content)
    out["holdings_csv_tw"] = _sha(build_holdings_csv(
        conn, now=now, reporting=twd, account="tw_broker").content)
    out["holdings_csv_us"] = _sha(build_holdings_csv(
        conn, now=now, reporting=twd, market=Market.US).content)
    out["holdings_report"] = _sha(build_holdings_report_html(
        conn, now=now, reporting=twd).content)
    out["holdings_report_schwab"] = _sha(build_holdings_report_html(
        conn, now=now, reporting=twd, account="schwab").content)
    out["realized_csv"] = _sha(strip_realized_additions(
        build_realized_csv(conn, now=now, reporting=twd).content))
    out["tax_2026"] = _zip_sha(build_tax_package_zip(
        conn, now=now, year=2026, reporting=twd).content)
    out["ledgers_report"] = _sha(build_ledgers_report_html(
        conn, now=now, frm=None, to=None).content)
    out["ledgers_report_range"] = _sha(build_ledgers_report_html(
        conn, now=now, frm="2026-02-01", to="2026-06-30").content)
    for account, ccy in (("schwab", None), ("tw_broker", Currency.TWD)):
        tag = f"{account}_{ccy.value if ccy else 'all'}"
        csv_art = build_cash_statement_csv(conn, account=account, ccy=ccy, now=now)
        html_art = build_cash_statement_report_html(conn, account=account, ccy=ccy, now=now)
        assert csv_art is not None and html_art is not None
        out[f"cash_csv_{tag}"] = _sha(csv_art.content)
        out[f"cash_report_{tag}"] = _sha(html_art.content)
    detail = build_symbol_detail_csv(conn, symbol="2330", today=now.date())
    assert detail is not None
    out["symbol_detail_2330"] = _sha(detail.content)
    out["rebalance_report"] = _sha(build_rebalance_report_html(
        conn, now=now, reporting=twd,
        targets={"2330": D("0.5"), "AAPL": D("0.3")}).content)
    return out


#: Recorded at ``7f23c51`` (before W6) with ``report_html.__version__`` pinned.
BEFORE: dict[str, str] = {
    "cash_csv_schwab_all": "5a17eb701ba1601fd363df57615a6da73babcb9d8b26e8389001b0f5374eeb9b",
    "cash_csv_tw_broker_TWD": "ca0ecca6614d6d4f128958a84431c2b112b733d2bfc551dc67a438b0ec44f410",
    "cash_report_schwab_all": "b4b4c5232299572277913e45f890fff3dfb8c892336c36bd94247f2bbbf8db4a",
    "cash_report_tw_broker_TWD":
        "ebaf7a7514b41d99f40732a2ff143c183dab4deb07ca579c22be881e217dfd11",
    "holdings_csv": "b6290f88b5fc42a1e534bb797f1c3836c279f8a6c9556b2c82a3f2aeb719cdbd",
    "holdings_csv_tw": "9539825e1fa8b8b0c1d6023525033bd4c83bd552d2e5352881bb9597cbe479f8",
    "holdings_csv_us": "693dce4db1ba1178cb6a39468ce89354c835708bebd9806f2f648968a3a14990",
    "holdings_report": "13fbc5a937c6063418c7dbbf708b296edce8bdc57c89aacc43110fca9212ecf5",
    "holdings_report_schwab": "a2bd5779c43c318004c9fbc48f8614cca5862ba70556356b5380238899bf217d",
    "ledgers_report": "ceb8423be9068b180d9c4bfae266a5c7fcd39d6eb1b394c0268f706ab5f2ab15",
    "ledgers_report_range": "b9df2ae909939de637639527b155f3e1f889eb882fdafe1a0c5ab2b668116424",
    "realized_csv": "ff59d9de581d6395583af24c94e14411f301a5e6c6db9a055053143467b15ef5",
    "rebalance_report": "213663a214d9e7c7a74d3135b7cb430bfa886d45c5f09329c8241e13a0ab3b1d",
    "symbol_detail_2330": "721e5446737faf8df04123d14d4d93fd5653e98b372a7d2b8200d221dc8784f0",
    "tax_2026": "e586ffcffdf155f1e7c812931d83a6ddcc6ce03a15b05d3adff34892dc8b4515",
}


@pytest.fixture
def pinned_version(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(report_html, "__version__", _PINNED_VERSION)
    yield


def test_every_stock_ledger_export_is_byte_identical_to_before(pinned_version: None) -> None:
    conn = stock_ledger()
    try:
        now = export_digests(conn)
    finally:
        conn.close()
    assert BEFORE, "the BEFORE digests must be recorded"
    changed = sorted(k for k in BEFORE if now.get(k) != BEFORE[k])
    assert not changed, f"stock-ledger exports changed: {changed}"
    assert set(now) == set(BEFORE)
