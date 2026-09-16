"""Unit tests for the read-time card figure check (M9, audit 2026-09-16).

``check_figures`` is PURE: card text + the stored input snapshot + the registered symbols
→ two capped lists. It never blocks and never rewrites a card, so the only thing to pin is
what it flags — and, just as load-bearing, what it must NOT flag. The first two tests are
the audit's own measured examples, verbatim.
"""

import json

from portfolio_dash.llm_insight.figure_check import MAX_FLAGS, check_figures

#: The snapshot the audit's two sibling cards were generated from: the same batch reported
#: 「未實現收益 4,290.80 美元，部位規模共 11,951 美元成本」 and 「未實現獲利 429.1 萬美元」.
_SNAPSHOT = json.dumps(
    {
        "kpis": {"unrealized_pnl": "4290.80", "cost_total": "11951"},
        "holdings": [{"symbol": "2330", "weight": "0.1249", "shares": 95}],
    },
    ensure_ascii=False,
)
_KNOWN = {"2330", "AAPL", "1155.KL"}


def test_scale_error_is_flagged_and_the_correct_sibling_is_not() -> None:
    """The measured ×1000: 「429.1 萬美元」 (= 4,291,000) against a snapshot holding 4290.80."""
    flags = check_figures("未實現獲利 429.1 萬美元", _SNAPSHOT, _KNOWN)
    assert flags.unverified_figures == ["429.1 萬"]

    ok = check_figures(
        "未實現收益 4,290.80 美元，部位規模共 11,951 美元成本", _SNAPSHOT, _KNOWN
    )
    assert ok.unverified_figures == []


def test_unknown_parenthesised_code_is_flagged_known_one_is_not() -> None:
    """The measured hallucination: 「LRDIM (6883)」 — a code held nowhere."""
    flags = check_figures("建議留意 LRDIM (6883) 的評價。", _SNAPSHOT, _KNOWN)
    assert flags.unknown_symbols == ["6883"]

    ok = check_figures("台積電 (2330) 仍是最大部位。", _SNAPSHOT, _KNOWN)
    assert ok.unknown_symbols == []


def test_percent_form_verifies_against_a_stored_ratio() -> None:
    # The snapshot stores weights as ratios; the card prints percentages.
    flags = check_figures("台積電權重 12.49%。", _SNAPSHOT, _KNOWN)
    assert flags.unverified_figures == []


def test_consistent_card_yields_empty_lists() -> None:
    """Positive control: every figure and code in this card is in the snapshot/registry."""
    text = (
        "持倉檢視\n"
        "未實現收益 4,290.80 美元，成本 11,951 美元。\n"
        "台積電 (2330) 權重 12.49%，共 95 股；近 5 日無重大變化，資料日 2026-09-16。"
    )
    flags = check_figures(text, _SNAPSHOT, _KNOWN)
    assert flags.unverified_figures == []
    assert flags.unknown_symbols == []


def test_counts_years_and_dates_are_not_figures() -> None:
    # Bare integers < 100, 4-digit years and date tokens are skipped even though this
    # snapshot contains none of them — they are not figures, so they cannot be "wrong".
    text = "分 3 個帳戶持有；2024 年以來，於 2026-09-16 與 9/30 檢視，持有 14 天。"
    assert check_figures(text, _SNAPSHOT, _KNOWN).unverified_figures == []


def test_common_abbreviations_are_never_read_as_tickers() -> None:
    text = "本益比（PE）偏高，以美元（USD）計價；(ETF) 部位不變，(AI) 題材延續。"
    assert check_figures(text, _SNAPSHOT, _KNOWN).unknown_symbols == []


def test_suffixed_known_symbol_matches_its_bare_code() -> None:
    # The ledger stores 1155.KL; a card writes （1155）. Flagging that would be exactly the
    # false positive this checker must not produce.
    assert check_figures("馬銀行（1155）配息穩定。", _SNAPSHOT, _KNOWN).unknown_symbols == []


def test_empty_or_unparseable_snapshot_flags_nothing() -> None:
    # "Cannot check" is not "wrong": a card whose snapshot holds no number must not be
    # painted 待核 for every figure it prints.
    for snapshot in ("", "{}", "not json at all"):
        flags = check_figures("未實現獲利 429.1 萬美元", snapshot, _KNOWN)
        assert flags.unverified_figures == [], snapshot


def test_sign_is_not_a_mismatch() -> None:
    # The check is about MAGNITUDE (scale), not sign: a card narrating a stored −3,200 as
    # 「虧損 3,200 美元」 is correct, and flagging it would teach the owner to ignore the pill.
    snapshot = json.dumps({"pnl": "-3200.00", "pct": "-0.125"})
    flags = check_figures("虧損 3,200.00 美元，跌 12.5%。", snapshot, _KNOWN)
    assert flags.unverified_figures == []


def test_each_list_is_capped() -> None:
    text = " ".join(f"{i}.5 萬美元" for i in range(1, 12))
    text += " " + " ".join(f"({6000 + i})" for i in range(1, 12))
    flags = check_figures(text, _SNAPSHOT, _KNOWN)
    assert len(flags.unverified_figures) == MAX_FLAGS
    assert len(flags.unknown_symbols) == MAX_FLAGS


def test_tolerance_absorbs_display_rounding_not_a_scale_error() -> None:
    snapshot = json.dumps({"v": "4290.804321"})
    assert check_figures("收益 4,290.80 美元", snapshot, _KNOWN).unverified_figures == []
    # 0.5% of 4,290 is ~21, so a 100-unit gap is outside the band by a factor of ~5.
    assert check_figures("收益 4,390.00 美元", snapshot, _KNOWN).unverified_figures == [
        "4,390.00"
    ]
