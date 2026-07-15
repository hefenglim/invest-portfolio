import sqlite3

from portfolio_dash.data_ingestion.resolve import ResolutionStatus, resolve
from portfolio_dash.data_ingestion.store import get_instrument, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument

_TSMC = Instrument(
    symbol="2330",
    market=Market.TW,
    quote_ccy=Currency.TWD,
    sector="Tech",
    name="台積電",
)


def test_instrument_roundtrip(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, _TSMC)
    upsert_instrument(conn, _TSMC)  # idempotent
    got = get_instrument(conn, "2330")
    assert got is not None and got.name == "台積電" and got.market is Market.TW


def test_resolve_exact_symbol(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, _TSMC)
    r = resolve(conn, "2330")
    assert r.status is ResolutionStatus.EXACT
    assert r.instrument is not None and r.instrument.symbol == "2330"


def test_resolve_fuzzy_by_name(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, _TSMC)
    r = resolve(conn, "台積電")
    assert r.status is ResolutionStatus.FUZZY
    assert r.instrument is not None and r.instrument.symbol == "2330"


def test_resolve_needs_ai_when_unknown(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, _TSMC)
    r = resolve(conn, "ZZ Unknown Corp")
    assert r.status is ResolutionStatus.NEEDS_AI and r.instrument is None


def test_weak_fuzzy_below_075_needs_ai(conn: sqlite3.Connection) -> None:
    """Audit L12: a 0.60-ratio near-miss used to resolve FUZZY; the 0.75 floor now
    sends it to NEEDS_AI (register/confirm) rather than silently accepting it.

    SequenceMatcher('abcde','abcxy').ratio() == 0.6 (matches 'abc'); the name 'Zzzzz'
    scores ~0 so the symbol ratio governs.
    """
    upsert_instrument(conn, Instrument(symbol="ABCDE", market=Market.US,
                                       quote_ccy=Currency.USD, sector="X", name="Zzzzz"))
    assert resolve(conn, "ABCXY").status is ResolutionStatus.NEEDS_AI
    # a strong (>=0.75) match still resolves
    assert resolve(conn, "ABCDX").status is ResolutionStatus.FUZZY
