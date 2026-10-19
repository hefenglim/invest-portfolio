"""Month and year closes (PERF plan §6.1 "Tests" — period closes; spec §11, §18 "Period closes").

- a year close is its December close (KPIs, positions, cash), and its flow lines are the sum of
  its months' lines, added in the replay's own row order;
- the activity ties out with the v0.1.31 tax package sheet by sheet (realized adjusted and
  original, dividends gross / withholding / net, realized FX) on every corpus it builds for;
- the year's time-weighted return is the product of its months' when every month end is
  complete;
- the notes say why: an open option, a missing or stale price, a missing or stale rate, a 待釐清
  position, an unconfirmed close; a dividend between its ex-date and its payment date is not
  activity yet; a split on the next trading day, on a weekend month end or on the month end
  leaves the month-end value unchanged.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from portfolio_dash.export.tax import build_tax_package_zip
from portfolio_dash.portfolio import period_close as pc
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.portfolio.settlement_models import PeriodActivity
from portfolio_dash.shared.enums import Currency
from tests.reference.corpora import CORPUS_NAMES, CorpusDB, open_db
from tests.settlement.conftest import CorpusFactory
from tests.settlement.test_worker import (
    PAYLOADS,
    generation,
    manifest,
    prepare,
    start_run,
)


def _closes(db: CorpusDB) -> tuple[int, list[st.SettledPeriod]]:
    conn = open_db(db.path)
    try:
        g = generation(conn)
        return g, st.read_periods(conn, g)
    finally:
        conn.close()


def _settled(db: CorpusDB) -> list[st.SettledPeriod]:
    prepare(db)
    assert start_run(db, 1) == "published"
    return _closes(db)[1]


# ---------------------------------------------------------------------------------------------
# year ≡ December, Σ months ≡ year
# ---------------------------------------------------------------------------------------------


def _flow_lines(a: PeriodActivity) -> dict[tuple[Any, ...], Decimal | None]:
    """Every flow line's native and reporting amount by its key (balances are not flows)."""
    out: dict[tuple[Any, ...], Decimal | None] = {}

    def put(key: tuple[Any, ...], value: Decimal | None) -> None:
        out[key] = value

    for r in a.realized:
        k: tuple[Any, ...] = ("realized", r.kind, r.asset_type, r.realized.ccy)
        put((*k, "n"), r.realized.native)
        put((*k, "r"), r.realized.reporting)
        if r.capital_gain is not None:
            put((*k, "cg"), r.capital_gain.native)
    for d in a.dividends:
        dk: tuple[Any, ...] = ("dividend", d.type, d.account_id)
        for name in ("gross", "withholding", "net"):
            m = getattr(d, name)
            if m is not None:
                put((*dk, m.ccy, name), m.native)
                put((*dk, m.ccy, name, "r"), m.reporting)
        if d.reinvested_shares is not None:
            put((*dk, "drip_shares"), d.reinvested_shares)
        if d.stock_shares is not None:
            put((*dk, "stock_shares"), d.stock_shares)
    for n in a.dividends_not_booked:
        put(("not_booked", n.net.ccy), n.net.native)
    for f in a.fees:
        put(("fees", f.account_id, f.fees.ccy), f.fees.native)
        put(("tax", f.account_id, f.tax.ccy), f.tax.native)
    for c in a.cash_movements:
        put(("cash", c.account_id, c.kind, c.amount.ccy), c.amount.native)
    for x in a.fx_conversions:
        put(("conv", x.account_id, x.from_amount.ccy, x.to_amount.ccy, "from"),
            x.from_amount.native)
        put(("conv", x.account_id, x.from_amount.ccy, x.to_amount.ccy, "to"),
            x.to_amount.native)
    for fx in a.fx_realized:
        put(("fx", fx.account_id, fx.realized.ccy), fx.realized.native)
    for o in a.openings:
        put(("opening", o.account_id, o.symbol), o.original_cost_total.native)
    return out


def _sum_months(months: list[st.SettledPeriod]) -> dict[tuple[Any, ...], Decimal | None]:
    """Σ of the months' lines, each key added left to right from zero in month order."""
    total: dict[tuple[Any, ...], Decimal | None] = {}
    for m in months:
        for key, value in _flow_lines(m.activity).items():
            if key not in total:
                total[key] = Decimal("0")
            prior = total[key]
            total[key] = None if prior is None or value is None else prior + value
    return total


@pytest.mark.parametrize("name", ["rich", "rich_refusals", "epoch1990"])
def test_a_year_is_its_december_and_the_sum_of_its_months(corpus_db: CorpusFactory,
                                                         name: str) -> None:
    db = corpus_db(name)
    closes = _settled(db)
    conn = open_db(db.path)
    try:
        gen = generation(conn)
        years = [c for c in closes if c.kind == "Y"]
        assert years, f"{name}: no year close"
        lines = 0
        for y in years:
            december = next(c for c in closes if c.kind == "M" and c.end_day == y.end_day)
            same = {"period_twr", "period_twr_reason", "notes"}
            for f in type(y.kpi).model_fields:
                if f not in same:
                    assert getattr(y.kpi, f) == getattr(december.kpi, f), f"{name} {f}"
            assert st.read_period_holdings(conn, gen, "Y", y.period) == \
                st.read_period_holdings(conn, gen, "M", december.period)
            assert st.read_period_cash(conn, gen, "Y", y.period) == \
                st.read_period_cash(conn, gen, "M", december.period)
            months = [c for c in closes if c.kind == "M" and c.end_day.year == y.end_day.year]
            mine = _flow_lines(y.activity)
            assert mine == _sum_months(months), f"{name} {y.period}"
            lines += sum(1 for v in mine.values() if v)
        assert lines, f"{name}: every year close is empty"
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------------
# the tax package tie-out, sheet by sheet
# ---------------------------------------------------------------------------------------------


def _csv(name: str, year: int, sheet: str) -> list[dict[str, str]]:
    raw = (PAYLOADS / name / f"tax_{year}" / f"{sheet}_{year}.csv").read_bytes()
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))


@pytest.mark.parametrize("name", [n for n in CORPUS_NAMES if n != "rich_refusals"])
def test_the_closes_tie_out_with_the_tax_package_sheet_by_sheet(corpus_db: CorpusFactory,
                                                               name: str) -> None:
    db = corpus_db(name)
    closes = _settled(db)
    tied = 0
    for year in manifest(name)["tax_years"]:
        months = [c for c in closes if c.kind == "M" and c.end_day.year == year]
        if not months:
            continue
        limit = months[-1].end_day
        mine = _sum_months(months)
        # realized gains: adjusted (performance) and original (filing), per ccy × asset type
        sheet: dict[tuple[str, str], list[Decimal]] = defaultdict(
            lambda: [Decimal("0"), Decimal("0")])
        for row in _csv(name, year, "realized_gains"):
            if date.fromisoformat(row["sell_date"]) > limit:
                continue
            acc = sheet[(row["quote_ccy"], row.get("asset_type") or "stock")]
            acc[0] += Decimal(row["realized_adjusted"])
            acc[1] += Decimal(row["realized_original"])
        got = {(k[3], k[2]): v for k, v in mine.items()
               if k[0] == "realized" and k[1] == "trade" and k[4] == "n"}
        got_cg = {(k[3], k[2]): v for k, v in mine.items()
                  if k[0] == "realized" and k[1] == "trade" and k[4] == "cg"}
        assert got == {k: v[0] for k, v in sheet.items()}, f"{name} {year} realized"
        assert got_cg == {k: v[1] for k, v in sheet.items()}, f"{name} {year} original"
        # dividends: gross / withholding / net per currency (booked CASH / NET / DRIP)
        divs: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
        for row in _csv(name, year, "dividends"):
            if date.fromisoformat(row["date"]) > limit:
                continue
            for col in ("gross", "withholding", "net"):
                divs[(row["ccy"], col)] += Decimal(row[col])
        mine_divs: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
        for k, v in mine.items():
            if k[0] == "dividend" and len(k) == 5 and v is not None:
                mine_divs[(str(k[3].value if isinstance(k[3], Currency) else k[3]), k[4])] += v
        assert {k: v for k, v in mine_divs.items() if v} == {k: v for k, v in divs.items()
                                                            if v}, f"{name} {year} dividends"
        # realized FX per account
        fx: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
        for row in _csv(name, year, "fx_realized"):
            if date.fromisoformat(row["date"]) > limit:
                continue
            fx[(row["account_id"], row["home_ccy"])] += Decimal(row["realized"])
        mine_fx = {(k[1], k[2].value): v for k, v in mine.items() if k[0] == "fx"}
        assert mine_fx == dict(fx), f"{name} {year} fx"
        tied += 1
    assert tied, f"{name}: no year tied out"


# ---------------------------------------------------------------------------------------------
# time-weighted return
# ---------------------------------------------------------------------------------------------


def test_a_years_twr_is_the_product_of_its_months(corpus_db: CorpusFactory) -> None:
    closes = _settled(corpus_db("epoch1990"))
    year = next(c for c in closes if c.kind == "Y" and c.period == "1989")
    months = [c for c in closes if c.kind == "M" and c.end_day.year == 1989]
    assert len(months) == 12 and all(not m.kpi.notes.incomplete for m in months)
    assert year.kpi.period_twr is not None
    product = Decimal("1")
    for m in months:
        assert m.kpi.period_twr is not None, m.period
        product *= Decimal("1") + m.kpi.period_twr
    assert abs((product - Decimal("1")) - year.kpi.period_twr) < Decimal("1E-20")
    first = next(c for c in closes if c.kind == "M")
    assert first.kpi.period_twr is not None  # the first period starts from the anchor


def test_twr_is_withheld_with_its_reason_on_an_incomplete_end(corpus_db: CorpusFactory
                                                             ) -> None:
    closes = _settled(corpus_db("golden"))  # an unpriced position at every month end
    for c in closes:
        assert c.kpi.notes.incomplete
        assert c.kpi.period_twr is None and c.kpi.period_twr_reason


# ---------------------------------------------------------------------------------------------
# notes
# ---------------------------------------------------------------------------------------------


def test_an_open_option_in_the_period_sets_options_value_excluded(corpus_db: CorpusFactory
                                                                 ) -> None:
    closes = {(c.kind, c.period): c for c in _settled(corpus_db("rich"))}
    assert not closes[("M", "2025-10")].kpi.notes.options_value_excluded
    assert not closes[("M", "2025-11")].kpi.notes.options_value_excluded
    for period in ("2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05"):
        assert closes[("M", period)].kpi.notes.options_value_excluded, period
    assert closes[("Y", "2025")].kpi.notes.options_value_excluded


def test_pending_positions_and_a_missing_price_are_noted(corpus_db: CorpusFactory) -> None:
    closes = {(c.kind, c.period): c for c in _settled(corpus_db("rich_refusals"))}
    march = closes[("M", "2026-03")].kpi.notes
    assert "NOPX" in march.missing_prices
    kinds = {(p.symbol, p.kind) for p in march.pending_positions}
    assert ("AAPL", "oversold") in kinds
    assert any(k == "refused_dividend" for _s, k in kinds)
    assert march.incomplete


def _x(db: CorpusDB, sql: str, params: tuple[Any, ...] = ()) -> None:
    conn = open_db(db.path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def test_a_stale_price_and_a_stale_rate_are_noted(corpus_db: CorpusFactory) -> None:
    db = corpus_db("demo")
    conn = open_db(db.path)
    try:
        sym = conn.execute("SELECT symbol FROM transactions t JOIN instruments i USING (symbol) "
                           "WHERE i.quote_ccy = 'USD' ORDER BY t.id LIMIT 1").fetchone()[0]
    finally:
        conn.close()
    _x(db, "DELETE FROM prices WHERE instrument = ? AND as_of_date BETWEEN '2026-04-20' AND "
           "'2026-04-30'", (sym,))
    _x(db, "DELETE FROM fx_rates WHERE ((base = 'USD' AND quote = 'TWD') OR (base = 'TWD' AND "
           "quote = 'USD')) AND as_of_date BETWEEN '2026-04-20' AND '2026-04-30'")
    april = next(c for c in _settled(db) if c.period == "2026-04")
    stale = {p.symbol: p.price_date for p in april.kpi.notes.stale_prices}
    price_date = stale.get(sym)
    assert price_date is not None and price_date < date(2026, 4, 20)
    assert "USD/TWD" in {s.pair for s in april.kpi.notes.stale_fx}
    used = {u.pair: u.as_of for u in april.kpi.fx_used}
    assert used["USD/TWD"] < date(2026, 4, 20)


def test_a_missing_rate_is_noted_and_blanks_the_market_value(corpus_db: CorpusFactory) -> None:
    db = corpus_db("golden")
    _x(db, "INSERT INTO instruments (symbol, market, quote_ccy, sector, name) "
           "VALUES ('MYX', 'MY', 'MYR', 'Energy', 'no rate')")
    _x(db, "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
           "trade_date) VALUES ('moomoo_my', 'MYX', 'BUY', '100', '2.50', '0', '0', "
           "'2026-02-02')")
    _x(db, "INSERT INTO prices (instrument, market, as_of_date, close, source, fetched_at) "
           "VALUES ('MYX', 'MY', '2026-02-02', '2.60', 'test', '2026-02-02T00:00:00')")
    closes = {c.period: c for c in _settled(db)}
    feb = closes["2026-02"].kpi
    assert "MYR/TWD" in feb.notes.missing_fx
    assert feb.total_market_value is None
    assert feb.trend_total_value is None  # the MYR buy has no rate: no trend from 02-02 on
    jan = closes["2026-01"].kpi
    assert jan.trend_total_value is not None and "MYR/TWD" not in jan.notes.missing_fx


def test_an_unconfirmed_close_carries_its_symbol_and_price_date(corpus_db: CorpusFactory
                                                               ) -> None:
    db = corpus_db("demo")
    friday = date(2026, 7, 31)
    conn = open_db(db.path)
    try:
        sym = conn.execute("SELECT symbol FROM transactions t JOIN instruments i USING (symbol) "
                           "WHERE i.market = 'US' ORDER BY t.id LIMIT 1").fetchone()[0]
    finally:
        conn.close()
    prepare(db)
    assert start_run(db, 1, unconfirmed={friday: [sym]}) == "published"
    july = next(c for c in _closes(db)[1] if c.period == "2026-07")
    [note] = july.kpi.notes.unconfirmed_closes
    assert note.symbol == sym and note.price_date is not None
    assert note.price_date <= friday


def test_a_weekend_month_end_carries_fridays_unconfirmed_close(corpus_db: CorpusFactory
                                                              ) -> None:
    """``RunRequest.unconfirmed_closes`` is keyed by the quote FIRE's trading day, not by the
    close: a Saturday month end (rich, 2026-01-31) is valued at Friday's 2330 close, so
    Friday's partial run marks it (spec §9.1 「never passes unmarked」). A key whose session the
    valued price is newer than does not (a later close was confirmed)."""
    db = corpus_db("rich")
    prepare(db)
    saturday, friday = date(2026, 1, 31), date(2026, 1, 30)
    assert start_run(db, 1, unconfirmed={friday: ["2330"], date(2026, 1, 29): ["AAPL"],
                                         date(2026, 2, 2): ["XYZ"]}) == "published"
    closes = {c.period: c for c in _closes(db)[1] if c.kind == "M"}
    january = closes["2026-01"]
    assert january.end_day == saturday
    assert [(n.symbol, n.price_date) for n in january.kpi.notes.unconfirmed_closes] == [
        ("2330", friday)]  # AAPL's price is dated 01-30, after its 01-29 key; XYZ's key > D
    assert not closes["2026-02"].kpi.notes.unconfirmed_closes  # newer closes since


def test_a_recompute_keeps_the_marks_its_request_no_longer_sees(corpus_db: CorpusFactory
                                                               ) -> None:
    """E builds the request from the ``job_runs`` its window still holds. A full run or the
    weekly self-check whose request lacks an old key keeps the previous generation's mark while
    the close is valued at the same price date — no spurious self-check difference, no mark
    dropped."""
    db = corpus_db("rich")
    prepare(db)
    friday = date(2026, 1, 30)
    assert start_run(db, 1, unconfirmed={friday: ["2330"]}) == "published"
    assert start_run(db, 2, scope="selfcheck", reason="selfcheck") == "published"
    conn = open_db(db.path)
    try:
        report = st.get_run(conn, 2)
        assert report is not None and report.selfcheck is not None
        assert report.selfcheck.differences == 0
    finally:
        conn.close()
    assert start_run(db, 3) == "published"  # a full run, no unconfirmed closes requested
    january = next(c for c in _closes(db)[1] if c.period == "2026-01" and c.kind == "M")
    assert [(n.symbol, n.price_date) for n in january.kpi.notes.unconfirmed_closes] == [
        ("2330", friday)]


def test_a_dividend_between_its_ex_date_and_its_payment_is_not_activity_yet(
        corpus_db: CorpusFactory) -> None:
    db = corpus_db("golden")
    _x(db, "INSERT INTO dividends (account_id, symbol, date, type, gross, withholding, net, "
           "ex_date) VALUES ('tw_broker', '2330', '2026-05-08', 'CASH', '3000', '0', '3000', "
           "'2026-04-28')")
    closes = {c.period: c for c in _settled(db)}
    april = closes["2026-04"]
    assert not any(d.net and d.net.native == Decimal("3000") for d in april.activity.dividends)
    assert april.kpi.notes.dividends_receivable is None  # computed when the close is read
    may = closes["2026-05"]
    assert any(d.net and d.net.native == Decimal("3000") for d in may.activity.dividends)


# ---------------------------------------------------------------------------------------------
# the first period starts at the ledger's first row
# ---------------------------------------------------------------------------------------------


def _tax_sheets(db: CorpusDB, year: int) -> dict[str, list[dict[str, str]]]:
    conn = open_db(db.path)
    try:
        art = build_tax_package_zip(conn, now=db.now, year=year, reporting=db.reporting)
    finally:
        conn.close()
    with zipfile.ZipFile(io.BytesIO(art.content)) as zf:
        return {info.filename: list(csv.DictReader(io.StringIO(
            zf.read(info).decode("utf-8-sig")))) for info in zf.infolist()
            if info.filename.endswith(".csv")}


def _fx_sheet(db: CorpusDB, year: int) -> dict[tuple[str, str], Decimal]:
    out: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for row in _tax_sheets(db, year)[f"fx_realized_{year}.csv"]:
        out[(row["account_id"], row["home_ccy"])] += Decimal(row["realized"])
    return dict(out)


def _fx_lines(close: st.SettledPeriod) -> dict[tuple[str, str], Decimal]:
    return {(x.account_id, x.realized.ccy.value): x.realized.native
            for x in close.activity.fx_realized}


def test_rows_before_the_first_trade_have_their_own_months(corpus_db: CorpusFactory) -> None:
    """``rich``'s trend starts 2025-10-01. A TWD deposit on 2025-03-03 and a TWD → USD → TWD
    round trip in April / May (a realized-FX row before the first trade): the closes start at
    the month of the ledger's first row, so each row is in its own month, the year is still the
    sum of its months, and it ties out with the tax package built for 2025."""
    db = corpus_db("rich")
    _x(db, "INSERT INTO cash_movements (account_id, date, kind, ccy, amount, note) "
           "VALUES ('tw_broker', '2025-03-03', 'DEPOSIT', 'TWD', '777777', 'probe')")
    _x(db, "INSERT INTO fx_conversions (account_id, date, from_ccy, from_amount, to_ccy, "
           "to_amount) VALUES ('schwab', '2025-04-01', 'TWD', '31000', 'USD', '1000')")
    _x(db, "INSERT INTO fx_conversions (account_id, date, from_ccy, from_amount, to_ccy, "
           "to_amount) VALUES ('schwab', '2025-05-02', 'USD', '500', 'TWD', '16000')")
    closes = _settled(db)
    year = next(c for c in closes if c.kind == "Y" and c.period == "2025")
    months = {c.period: c for c in closes if c.kind == "M" and c.end_day.year == 2025}
    assert min(months) == "2025-03"
    assert _flow_lines(year.activity) == _sum_months(sorted(months.values(),
                                                            key=lambda c: c.end_day))
    march = months["2025-03"]
    assert [(m.account_id, m.amount.native) for m in march.activity.cash_movements] == [
        ("tw_broker", Decimal("777777"))]
    assert march.kpi.trend_total_value is None and march.kpi.period_twr is None
    assert march.kpi.notes.trend_unavailable == pc.TREND_NOT_STARTED_ZH
    assert march.kpi.period_twr_reason == pc.TWR_NOT_STARTED_ZH
    assert _fx_lines(months["2025-05"]) == {("schwab", "TWD"): Decimal("500")}
    assert _fx_lines(year) == _fx_sheet(db, 2025) == {("schwab", "TWD"): Decimal("500")}
    realized: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for row in _tax_sheets(db, 2025)["realized_gains_2025.csv"]:
        realized[(row["quote_ccy"], row.get("asset_type") or "stock")] += Decimal(
            row["realized_adjusted"])
    mine = {(k[3].value, k[2]): v for k, v in _flow_lines(year.activity).items()
            if k[0] == "realized" and k[1] == "trade" and k[4] == "n"}
    assert mine == dict(realized)


def test_a_first_row_in_an_earlier_year_has_its_own_year_close(corpus_db: CorpusFactory
                                                              ) -> None:
    """The fold verification's scenario G: rich plus a 2024 deposit and a 2024 TWD → USD → TWD
    round trip — an account funded the year before its first trade. The 2024 year close holds
    the 500 of realized FX and the deposit, the 2025 one holds neither, and each ties out with
    its own year's ``fx_realized_<year>.csv``."""
    db = corpus_db("rich")
    _x(db, "INSERT INTO fx_conversions (account_id, date, from_ccy, from_amount, to_ccy, "
           "to_amount) VALUES ('schwab', '2024-04-01', 'TWD', '31000', 'USD', '1000')")
    _x(db, "INSERT INTO fx_conversions (account_id, date, from_ccy, from_amount, to_ccy, "
           "to_amount) VALUES ('schwab', '2024-05-02', 'USD', '500', 'TWD', '16000')")
    _x(db, "INSERT INTO cash_movements (account_id, date, kind, ccy, amount, note) "
           "VALUES ('tw_broker', '2024-03-03', 'DEPOSIT', 'TWD', '777777', 'probe')")
    closes = _settled(db)
    years = {c.period: c for c in closes if c.kind == "Y"}
    assert set(years) == {"2024", "2025"}
    y2024, y2025 = years["2024"], years["2025"]
    assert _fx_lines(y2024) == _fx_sheet(db, 2024) == {("schwab", "TWD"): Decimal("500")}
    assert _fx_lines(y2025) == _fx_sheet(db, 2025) == {}
    assert [(m.account_id, m.amount.native) for m in y2024.activity.cash_movements
            if m.kind == "DEPOSIT"] == [("tw_broker", Decimal("777777"))]
    assert all(m.amount.native != Decimal("777777") for m in y2025.activity.cash_movements)
    for year in ("2024", "2025"):
        months = sorted((c for c in closes if c.kind == "M" and c.period.startswith(year)),
                        key=lambda c: c.end_day)
        assert _flow_lines(years[year].activity) == _sum_months(months), year
    assert y2024.kpi.notes.trend_unavailable == pc.TREND_NOT_STARTED_ZH
    conn = open_db(db.path)
    try:
        g = generation(conn)
        cash = {(c.account_id, c.ccy.value): c.balance
                for c in st.read_period_cash(conn, g, "Y", "2024")}
        assert cash[("tw_broker", "TWD")] == Decimal("777777")
        assert st.store_state(conn, reporting=db.reporting).first_day == date(2025, 10, 1)
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------------
# splits around a month end
# ---------------------------------------------------------------------------------------------


def _split(db: CorpusDB, symbol: str, day: date) -> None:
    """A 2-for-1 SPLIT on *day*, the stored closes from *day* on as traded (halved)."""
    conn = open_db(db.path)
    try:
        account = conn.execute("SELECT account_id FROM transactions WHERE symbol = ? "
                               "ORDER BY id LIMIT 1", (symbol,)).fetchone()[0]
        conn.execute("INSERT INTO corporate_actions (account_id, date, kind, from_symbol, "
                     "to_symbol, ratio_to, ratio_from) VALUES (?, ?, 'SPLIT', ?, ?, '2', '1')",
                     (account, day.isoformat(), symbol, symbol))
        for as_of, close in conn.execute(
                "SELECT as_of_date, close FROM prices WHERE instrument = ? AND as_of_date >= ?",
                (symbol, day.isoformat())).fetchall():
            conn.execute("UPDATE prices SET close = ? WHERE instrument = ? AND as_of_date = ?",
                         (str(Decimal(close) / 2), symbol, as_of))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(("end", "split_on"), [
    (date(2026, 5, 31), date(2026, 6, 1)),   # the first trading day after a weekend month end
    (date(2026, 5, 31), date(2026, 5, 31)),  # on a weekend month end
    (date(2026, 4, 30), date(2026, 4, 30)),  # on a trading month end
    (date(2026, 4, 30), date(2026, 5, 1)),   # the next trading day
])
def test_a_split_around_the_month_end_leaves_its_value_unchanged(
        corpus_db: CorpusFactory, end: date, split_on: date) -> None:
    base = corpus_db("demo")
    conn = open_db(base.path)
    try:
        sym = conn.execute("SELECT t.symbol FROM transactions t JOIN instruments i USING (symbol) "
                           "WHERE i.market = 'TW' AND t.trade_date < '2026-03-01' "
                           "ORDER BY t.id LIMIT 1").fetchone()[0]
    finally:
        conn.close()
    plain = {c.period: c for c in _settled(base)}[f"{end:%Y-%m}"]
    db = corpus_db("demo")
    _split(db, sym, split_on)
    closes = {c.period: c for c in _settled(db)}
    split = closes[f"{end:%Y-%m}"]
    assert not closes["2026-06"].kpi.notes.pending_positions  # the split was applied
    assert split.kpi.total_market_value == plain.kpi.total_market_value
    assert split.kpi.trend_total_value == plain.kpi.trend_total_value
