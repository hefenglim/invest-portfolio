"""shared/options.py — the ONE owner of the OCC symbol, the multiplier and the §6.1 vocabulary.

Spec ``docs/spec/2026-10-09-options-ledger.md`` §3 (symbol, terms), §6.1 (vocabulary and the
classifier), OPT-D11 / OPT-D16 (standard adjustments only). Synthetic contracts only.
"""

from __future__ import annotations

import ast
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from portfolio_dash.shared import options as opt
from portfolio_dash.shared.ledger_events import EventPriority
from portfolio_dash.shared.options import (
    MULTIPLIER,
    OptionAdjustRow,
    OptionRow,
    OptionSide,
    OptionSymbolError,
    OptionTerms,
    classify_option_rows,
    contracts_to_shares,
    from_broker_option,
    is_contract_multiple,
    is_standard_adjustment,
    nonstandard_adjustment_reason,
    option_display_name,
    option_symbol,
    parse_option_symbol,
    shares_to_contracts,
)

_PUT = OptionTerms(underlying="XYZ", expiry=date(2026, 12, 18), strike=Decimal("50"),
                   right="P")


# --- symbol: parse / compose / display -------------------------------------------------


def test_parse_reads_every_term() -> None:
    t = parse_option_symbol("XYZ261218P00050000")
    assert t == _PUT
    assert t is not None and t.multiplier == MULTIPLIER == 100


def test_parse_normalises_case_and_padding() -> None:
    assert parse_option_symbol("  xyz261218p00050000 ") == _PUT


@pytest.mark.parametrize("raw, strike", [
    ("XYZ261218C00007500", Decimal("7.5")),
    ("XYZ261218C00100000", Decimal("100")),
    ("XYZ261218C00012345", Decimal("12.345")),
])
def test_parse_strike_is_exact(raw: str, strike: Decimal) -> None:
    t = parse_option_symbol(raw)
    assert t is not None and t.strike == strike and t.right == "C"


@pytest.mark.parametrize("raw", [
    "XYZ",                      # a stock
    "2330",                     # a TW code
    "2330261218P00050000",      # digit root: TW / MY options are out (R6)
    "XYZ1261218P00050000",      # an adjusted (non-standard) root carries a digit
    "XYZ261318P00050000",       # month 13
    "XYZ261218X00050000",       # neither C nor P
    "XYZ261218P00000000",       # strike 0
    "ABCDEFG261218P00050000",   # root longer than six letters
    "",
])
def test_parse_refuses_anything_else(raw: str) -> None:
    assert parse_option_symbol(raw) is None


def test_compose_is_the_inverse_of_parse() -> None:
    assert option_symbol(_PUT) == "XYZ261218P00050000"
    for raw in ("XYZ261218C00007500", "A270115P00100000", "ABCDEF261218C00012345"):
        terms = parse_option_symbol(raw)
        assert terms is not None and option_symbol(terms) == raw


def test_compose_normalises_the_root() -> None:
    assert option_symbol(OptionTerms(" xyz ", date(2026, 12, 18), Decimal("50"), "P")) == (
        "XYZ261218P00050000")


@pytest.mark.parametrize("terms, words", [
    (OptionTerms("2330", date(2026, 12, 18), Decimal("50"), "P"), "美股"),
    (OptionTerms("BRK.B", date(2026, 12, 18), Decimal("50"), "P"), "英文字母"),
    (OptionTerms("XYZ", date(2026, 12, 18), Decimal("0"), "P"), "履約價"),
    (OptionTerms("XYZ", date(2026, 12, 18), Decimal("50.0001"), "P"), "履約價"),
    (OptionTerms("XYZ", date(2026, 12, 18), Decimal("100000"), "P"), "履約價"),
    (OptionTerms("XYZ", date(2026, 12, 18), Decimal("50"), "X"), "買權"),  # type: ignore[arg-type]
    (OptionTerms("XYZ", date(2026, 12, 18), Decimal("50"), "P", multiplier=10), "100"),
    (OptionTerms("XYZ", date(2101, 1, 1), Decimal("50"), "P"), "到期日"),
])
def test_compose_refuses_in_chinese(terms: OptionTerms, words: str) -> None:
    with pytest.raises(OptionSymbolError) as exc:
        option_symbol(terms)
    assert words in str(exc.value)
    assert isinstance(exc.value, ValueError)


def test_display_name() -> None:
    assert option_display_name(_PUT) == "XYZ 2026-12-18 50 Put"
    call = parse_option_symbol("XYZ261218C00007500")
    assert call is not None and option_display_name(call) == "XYZ 2026-12-18 7.5 Call"


@pytest.mark.parametrize("raw, symbol", [
    ("XYZ 12/18/2026 50.00 P", "XYZ261218P00050000"),
    ("AAA 01/16/2026 7.50 C", "AAA260116C00007500"),
    ("  aaa  01/16/2026  7.5  c ", "AAA260116C00007500"),
])
def test_broker_string(raw: str, symbol: str) -> None:
    terms = from_broker_option(raw)
    assert terms is not None and option_symbol(terms) == symbol


@pytest.mark.parametrize("raw", [
    "XYZ", "XYZ1 12/18/2026 50.00 P", "2330 12/18/2026 50.00 P", "XYZ 13/18/2026 50.00 P",
    "XYZ 12/18/2026 50.00 Q", "XYZ 12/18/2026 0.00 P",
])
def test_broker_string_refuses(raw: str) -> None:
    assert from_broker_option(raw) is None


# --- units -------------------------------------------------------------------------------


def test_units_live_here() -> None:
    assert contracts_to_shares(Decimal("2")) == Decimal("200")
    assert contracts_to_shares(3) == Decimal("300")
    assert shares_to_contracts(Decimal("200")) == Decimal("2")
    assert is_contract_multiple(Decimal("300")) and not is_contract_multiple(Decimal("250"))
    assert not is_contract_multiple(Decimal("0")) and not is_contract_multiple(Decimal("-100"))


# --- OPT-D11 / OPT-D16: standard adjustments only ----------------------------------------


def _t(raw: str) -> OptionTerms:
    terms = parse_option_symbol(raw)
    assert terms is not None
    return terms


_SRC = _t("XYZ261218P00060000")


def test_one_to_three_is_standard() -> None:
    assert is_standard_adjustment(_SRC, _t("XYZ261218P00020000"), Decimal(3), Decimal(1))
    # a renamed root keeps the terms: still standard
    assert is_standard_adjustment(_SRC, _t("XYZN261218P00060000"), Decimal(1), Decimal(1))


@pytest.mark.parametrize("to, ratio_to, ratio_from, words", [
    ("XYZ261218P00040000", 3, 2, "1 口"),         # 3-for-2: a fractional contract
    ("XYZ261218P00180000", 1, 3, "1 口"),         # reverse
    ("XYZ261218P00020000", 6, 2, "1 口"),         # ratio_from > 1 even when reducible
    ("XYZ261218C00020000", 3, 1, "買權"),          # put → call
    ("XYZ270115P00020000", 3, 1, "到期日"),        # changed expiry
    ("XYZ261218P00025000", 3, 1, "履約價"),        # strike × ratio not conserved
    ("XYZ261218P00020000", 0, 1, "正整數"),        # ratio_to 0
])
def test_non_standard_is_refused(to: str, ratio_to: int, ratio_from: int, words: str) -> None:
    reason = nonstandard_adjustment_reason(_SRC, _t(to), Decimal(ratio_to), Decimal(ratio_from))
    assert reason is not None and words in reason
    assert not is_standard_adjustment(_SRC, _t(to), Decimal(ratio_to), Decimal(ratio_from))


def test_a_non_standard_multiplier_is_refused() -> None:
    odd = OptionTerms("XYZ", date(2026, 12, 18), Decimal("20"), "P", multiplier=150)
    assert not is_standard_adjustment(_SRC, odd, Decimal(3), Decimal(1))


def test_digit_root_destination_cannot_even_be_parsed() -> None:
    # OPT-D11's letters-only destination root is the parse itself: an adjusted root such as
    # XYZ1 never becomes OptionTerms, so it never reaches the adjustment check.
    assert parse_option_symbol("XYZ1261218P00020000") is None


# --- §6.1 classifier ---------------------------------------------------------------------

_D = date(2026, 3, 2)
_C = "XYZ261218P00050000"


def _row(i: int, side: str, qty: int, *, day: date = _D, short: bool = False,
         close: str | None = None, symbol: str = _C, account: str = "schwab") -> OptionRow:
    return OptionRow(id=i, account_id=account, symbol=symbol, trade_date=day, side=side,
                     quantity=Decimal(qty), short_sale=short, close_reason=close)


def test_open_and_close_both_directions() -> None:
    rows = [
        _row(1, "SELL", 200, short=True),                       # 賣出開倉
        _row(2, "BUY", 100),                                    # 買進平倉
        _row(3, "BUY", 300, symbol="XYZ261218C00050000"),       # 買進開倉
        _row(4, "SELL", 100, symbol="XYZ261218C00050000"),      # 賣出平倉
    ]
    assert classify_option_rows(rows, []) == {
        1: OptionSide.STO, 2: OptionSide.BTC, 3: OptionSide.BTO, 4: OptionSide.STC}


def test_over_cover_and_over_sell_labels() -> None:
    rows = [
        _row(1, "SELL", 100, short=True),
        _row(2, "BUY", 300),                                     # covers 1, opens 2
        _row(3, "SELL", 300, short=True),                        # sells 2 long, opens 1 short
        _row(4, "BUY", 100),                                     # covers the 1 short
        _row(5, "SELL", 100),                                    # undeclared, flat → 賣超
    ]
    got = classify_option_rows(rows, [])
    assert got == {1: OptionSide.STO, 2: OptionSide.BTC_BTO, 3: OptionSide.STC_STO,
                   4: OptionSide.BTC, 5: OptionSide.STC_OVERSOLD}


def test_a_declared_sell_within_the_long_is_a_plain_close() -> None:
    rows = [_row(1, "BUY", 200), _row(2, "SELL", 200, short=True)]
    assert classify_option_rows(rows, []) == {1: OptionSide.BTO, 2: OptionSide.STC}


def test_close_reasons() -> None:
    put, call = _C, "XYZ261218C00050000"
    rows = [
        _row(1, "SELL", 200, short=True, symbol=put),
        _row(2, "BUY", 100, close="assigned", symbol=put, day=date(2026, 12, 10)),
        _row(3, "BUY", 100, close="expired", symbol=put, day=date(2026, 12, 18)),
        _row(4, "BUY", 100, symbol=call),
        _row(5, "SELL", 100, close="exercised", symbol=call, day=date(2026, 12, 11)),
    ]
    assert classify_option_rows(rows, []) == {
        1: OptionSide.STO, 2: OptionSide.ASSIGNED, 3: OptionSide.EXPIRED,
        4: OptionSide.BTO, 5: OptionSide.EXERCISED}


def test_a_close_the_replay_refuses_is_unbooked_and_moves_nothing() -> None:
    rows = [
        _row(1, "BUY", 100, close="expired"),                   # no position at all
        _row(2, "SELL", 100, short=True),
        _row(3, "BUY", 200, close="expired"),                    # beyond the short lot
        _row(4, "SELL", 100, close="expired"),                   # against the direction
        _row(5, "BUY", 100, close="expired"),                    # still books: 1 short open
    ]
    assert classify_option_rows(rows, []) == {
        1: OptionSide.UNBOOKED, 2: OptionSide.STO, 3: OptionSide.UNBOOKED,
        4: OptionSide.UNBOOKED, 5: OptionSide.EXPIRED}


def test_an_adjustment_moves_the_count_before_the_days_trades() -> None:
    src, new = "XYZ261218P00060000", "XYZ261218P00020000"   # 60 × 1 = 20 × 3
    rows = [
        _row(1, "SELL", 100, short=True, symbol=src),
        _row(2, "BUY", 300, symbol=new, day=date(2026, 6, 1)),  # same day as the adjustment
    ]
    adj = [OptionAdjustRow(id=7, account_id="schwab", date=date(2026, 6, 1),
                           from_symbol=src, to_symbol=new, ratio_to=Decimal(3))]
    # STO 1 口 → 1→3 → BUY 3 口 = 買進平倉 (the adjustment runs before the day's trades)
    assert classify_option_rows(rows, adj) == {1: OptionSide.STO, 2: OptionSide.BTC}


def test_a_refused_adjustment_moves_nothing() -> None:
    src, new = "XYZ261218P00060000", "XYZ261218P00020000"
    rows = [
        _row(1, "SELL", 100, short=True, symbol=src),
        _row(2, "BUY", 100, symbol=new),                         # destination long
        _row(3, "BUY", 300, symbol=new, day=date(2026, 6, 2)),
    ]
    adj = [OptionAdjustRow(id=7, account_id="schwab", date=date(2026, 6, 1),
                           from_symbol=src, to_symbol=new, ratio_to=Decimal(3))]
    # short source → long destination is the opposite direction: refused, nothing moves,
    # so the later BUY on `new` still adds to the long — 買進開倉, not 買進平倉.
    assert classify_option_rows(rows, adj)[3] == OptionSide.BTO


def test_an_adjustment_with_no_source_or_an_oversold_side_moves_nothing() -> None:
    src, new = "XYZ261218P00060000", "XYZ261218P00020000"
    no_source = [OptionAdjustRow(1, "schwab", date(2026, 6, 1), src, new, Decimal(3))]
    later = [_row(9, "BUY", 300, symbol=new, day=date(2026, 6, 2))]
    assert classify_option_rows(later, no_source) == {9: OptionSide.BTO}

    oversold = [_row(1, "BUY", 100, symbol=src), _row(2, "SELL", 200, symbol=src),
                _row(3, "BUY", 200, symbol=src),                 # nets long again, still 賣超
                _row(9, "SELL", 300, short=True, symbol=new, day=date(2026, 6, 2))]
    got = classify_option_rows(oversold, no_source)
    assert got[2] == OptionSide.STC_OVERSOLD
    assert got[9] == OptionSide.STO                              # nothing was carried over


def test_accounts_walk_apart() -> None:
    rows = [_row(1, "SELL", 100, short=True, account="schwab"),
            _row(2, "BUY", 100, account="moomoo_my")]
    assert classify_option_rows(rows, []) == {1: OptionSide.STO, 2: OptionSide.BTO}


def test_same_day_rows_walk_in_id_order_whatever_the_list_order() -> None:
    rows = [_row(2, "BUY", 100), _row(1, "SELL", 100, short=True)]
    assert classify_option_rows(rows, []) == {1: OptionSide.STO, 2: OptionSide.BTC}


def test_the_local_order_matches_the_replays() -> None:
    assert (opt._ACTION_RANK < opt._TRADE_RANK) == (
        EventPriority.CORPORATE_ACTION < EventPriority.TRADE)


# --- §6.1 vocabulary guard -----------------------------------------------------------------

_SIX_ONE = {
    "STO": "賣出開倉", "BTC": "買進平倉", "BTO": "買進開倉", "STC": "賣出平倉",
    "BTC_BTO": "買進平倉＋買進開倉", "STC_STO": "賣出平倉＋賣出開倉",
    "STC_OVERSOLD": "賣出平倉（賣超）", "EXPIRED": "到期作廢", "ASSIGNED": "被指派",
    "EXERCISED": "履約", "UNBOOKED": "未入帳（待釐清）",
}


def test_option_side_is_the_spec_table() -> None:
    assert {s.name: s.value for s in OptionSide} == _SIX_ONE


def test_the_other_rows_of_the_table() -> None:
    assert opt.DIRECTION_ZH == {"long": "買方", "short": "賣方"}
    assert (opt.UNIT_CONTRACT, opt.UNIT_SHARE_EQUIVALENT) == ("口", "股當量")
    assert (opt.PREMIUM_ZH, opt.STRIKE_ZH, opt.OBLIGATION_ZH) == (
        "權利金（每股）", "履約價", "履約義務")
    assert opt.RIGHT_ZH == {"C": "買權", "P": "賣權"}
    assert opt.CLOSE_REASON_ZH == {"expired": "到期作廢", "assigned": "被指派",
                                   "exercised": "履約"}
    assert opt.ADJUST_KIND_ZH == "選擇權合約調整"


def test_notes_are_the_specs_words() -> None:
    assert opt.TREND_EXCLUDES_OPTIONS_NOTE == (
        "走勢不含未平倉選擇權的市值；權利金已計入現金，未平倉期間淨值高估（賣方）或低估（買方）該部位市值")
    assert opt.PRINCIPAL_FX_OPTION_NOTE == (
        "有未平倉選擇權時，含匯兌總損益不含其市值而資產損益含其未實現，兩者相減無法拆出本金匯率效果；平倉或到期後恢復顯示")
    assert opt.REFUSED_CLOSE_REMEDY == "刪除該筆到期作廢／被指派／履約列，或補回被刪除的交易"


# --- layering -------------------------------------------------------------------------------


def test_shared_options_imports_nothing_internal() -> None:
    src = Path(opt.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("portfolio_dash"), node.module
        elif isinstance(node, ast.Import):
            assert not any(a.name.startswith("portfolio_dash") for a in node.names)


def test_the_deferred_approach_is_marked() -> None:
    assert "TODO(options-⑤)" in Path(opt.__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize("value", ["1", "3", "0", "-1", "1.5", "2.0", "NaN", "Infinity"])
def test_the_local_ratio_rule_is_corporate_actions(value: str) -> None:
    from portfolio_dash.shared.corporate_actions import is_ratio_term

    assert opt._is_positive_integer(Decimal(value)) is is_ratio_term(Decimal(value))


def test_asset_type_words() -> None:
    assert opt.ASSET_TYPE_ZH == {"stock": "股票", "option": "選擇權"}
