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


# --- re-verification 2026-09-17 (the audit author's ❌ on M9) ---------------------------


def test_the_real_legacy_row_is_reported_as_unchecked_not_clean() -> None:
    """The audit's #37 card, with the population it REALLY has on the demo database.

    Every one of the 149 stored cards holds the fingerprint fallback ``"<date>|<target>"``
    as its snapshot (no caller ever fed ``RunInputs.input_snapshots``), so the first version
    of this check returned a clean ``[]`` for the ×1000 card — "cannot check" read as
    "checked". The state is now its own value, and it is the state the page renders.
    """
    flags = check_figures(
        "美股部位：科技雙巨頭領漲\n美股部位淨值 202.4 萬美元，未實現獲利 429.1 萬美元",
        "2026-07-05|US", _KNOWN,
    )
    assert flags.snapshot == "none"
    assert flags.unverified_figures == []  # nothing was compared, so nothing is "wrong"

    # A legacy card that prints no figure at all has nothing to check: vacuously checked,
    # so the pill appears exactly where it carries information.
    narrative = check_figures("科技股走勢偏多，建議持續觀察。", "2026-07-05|US", _KNOWN)
    assert narrative.snapshot == "checked"
    assert narrative.unverified_figures == []

    # The symbol check does not need a population and still runs on a legacy row.
    both = check_figures("留意 LRDIM (6883)，獲利 429.1 萬美元", "2026-07-05|US", _KNOWN)
    assert both.snapshot == "none" and both.unknown_symbols == ["6883"]


def test_prompt_figures_json_is_the_population_the_model_saw() -> None:
    """The population is extracted from the exact prompt string, at the scale it was fed."""
    from portfolio_dash.llm_insight.figure_check import prompt_figures_json

    prompt = (
        "<kpis>未實現損益 4,290.80 USD；成本 11951；權重 12.49%；營收 120 億；"
        "資料日 2026-09-16</kpis> 請回 JSON。"
    )
    population = json.loads(prompt_figures_json(prompt))
    assert "4290.80" in population and "11951" in population
    assert "12.49%" in population  # kept as a percent form; the reader expands it
    assert "12000000000" in population  # 120 億, scaled once at extraction
    assert population == list(dict.fromkeys(population))  # de-duplicated, order kept

    # End to end through the reader: the ×1000 card is flagged, its correct sibling is not.
    bad = check_figures("未實現獲利 429.1 萬美元", prompt_figures_json(prompt), _KNOWN)
    assert bad.snapshot == "checked" and bad.unverified_figures == ["429.1 萬"]
    good = check_figures(
        "未實現收益 4,290.80 美元，權重 12.49%，營收 120 億", prompt_figures_json(prompt), _KNOWN
    )
    assert good.snapshot == "checked" and good.unverified_figures == []


def test_period_suffixed_indicators_ratios_ratings_and_indices_are_not_tickers() -> None:
    """Measured 2026-09-17 on the demo: 47 of 48 unknown_symbols hits were these tokens."""
    text = (
        "股價站上 (MA20)、(MA60)、(MA120)、(MA200)、(MA50)，(RSI14) 中性，(PBR) 偏高；"
        "評等 (BUY) 轉 (HOLD)；對比 (KLCI) 與 (TAIEX)；(EMA12) 與 (KD9) 交叉。"
    )
    assert check_figures(text, _SNAPSHOT, _KNOWN).unknown_symbols == []
    # …and the one real hallucination in that same measurement is still caught.
    assert check_figures(text + " 留意 LRDIM (6883)。", _SNAPSHOT, _KNOWN).unknown_symbols == [
        "6883"
    ]


def test_five_and_six_digit_tw_codes_are_codes_not_figures() -> None:
    """「（00878）」 read as the number 878 would be an unverified figure on every TW ETF."""
    flags = check_figures("高股息 (00878) 與 (006208) 的配置。", _SNAPSHOT, _KNOWN)
    assert flags.unverified_figures == []
    assert flags.unknown_symbols == ["00878", "006208"]  # unregistered here → symbol check
    known = check_figures("高股息 (00878) 的配置。", _SNAPSHOT, _KNOWN | {"00878.TW"})
    assert known.unknown_symbols == []


def test_the_cards_own_prediction_is_not_a_hallucination() -> None:
    """Second re-verification (2026-09-17), the audit author's #175: the body echoed
    「target_pct: 0.05」 — the card's OWN stored prediction — and wore 數值待核. A forecast is
    the one number the model legitimately originates, so the stored fields join the
    population through ``card_own_figures``. Tied to the STORED fields, never to the word."""
    from decimal import Decimal

    from portfolio_dash.llm_insight.figure_check import card_own_figures, prompt_figures_json

    prompt = "<position>市價 215.12；均價 139.99；帳面獲利 1502.56</position>"
    body = "- **prediction**：\n  - target_pct: 0.05\n  - horizon_days: 14\n- **confidence**：70"

    # without the card's declared prediction, 0.05 IS a number nobody fed it
    bare = check_figures(body, prompt_figures_json(prompt), _KNOWN)
    assert bare.unverified_figures == ["0.05"]

    own = card_own_figures(target_pct=Decimal("0.05"), horizon_days=14, confidence=70)
    assert own == [Decimal("0.05"), Decimal(14), Decimal(70)]
    declared = check_figures(body, prompt_figures_json(prompt), _KNOWN, own_figures=own)
    assert declared.snapshot == "checked" and declared.unverified_figures == []
    # the same forecast written as a percent
    pct = check_figures("預期兩週內上漲 5%", prompt_figures_json(prompt), _KNOWN, own_figures=own)
    assert pct.unverified_figures == []
    # a card that declares nothing contributes nothing
    assert card_own_figures(target_pct=None, horizon_days=None, confidence=None) == []
    # own figures never manufacture a population: a legacy card stays "none"
    legacy = check_figures(body, "2026-07-05|US", _KNOWN, own_figures=own)
    assert legacy.snapshot == "none" and legacy.unverified_figures == []


def test_a_real_scale_error_on_a_fresh_card_stays_flagged() -> None:
    """The audit author's #172 (8299) — 「無法判定」 in the report, decidable from the stored
    population: the prompt fed ``16706711`` and ``497427``; the card wrote 「外資淨買超 1,670.67
    萬，近 20 日淨買超 497.43 萬」. The first is right, the second is ×10. The check's first
    real catch on a freshly generated card; pinned so a later relaxation cannot lose it."""
    from portfolio_dash.llm_insight.figure_check import prompt_figures_json

    prompt = '{"foreign_net":"16706711","foreign_net_20d":"497427","margin_change":"12385"}'
    population = prompt_figures_json(prompt)
    flags = check_figures("外資淨買超 1,670.67 萬，近 20 日淨買超 497.43 萬", population, _KNOWN)
    assert flags.unverified_figures == ["497.43 萬"]
    right = check_figures("外資淨買超 1,670.67 萬，近 20 日淨買超 49.74 萬", population, _KNOWN)
    assert right.unverified_figures == []


def test_cross_account_totals_come_from_the_prompt_not_from_arithmetic() -> None:
    """The audit author's #173 (AAPL): 「總計 95.0457 股，總成本約為 15,916.00 USD」 was the
    model's correct sum of two per-account rows — and no fed number said so. The fix is
    upstream (``variables._combined_block`` hands the model the totals), which this test
    shows from the checker's side: the same card against the population WITH the combined
    block verifies. Pairwise sums here were rejected (they would accept almost anything)."""
    from portfolio_dash.llm_insight.figure_check import prompt_figures_json

    per_account = (
        '{"positions":[{"shares":"85.03925471251653744415967210","original_cost_total":"13515"},'
        '{"shares":"10.00645756457564575645756458","original_cost_total":"2401"}]}'
    )
    card = "持有 Apple (AAPL) 總計 95.0457 股，總成本約為 15,916.00 USD。"
    before = check_figures(card, prompt_figures_json(per_account), _KNOWN)
    assert before.unverified_figures == ["95.0457", "15,916.00"]

    with_combined = per_account[:-1] + (
        ',"combined":{"account_count":2,"shares":"95.04571227709218320061723668",'
        '"original_cost_total":"15916"}}'
    )
    after = check_figures(card, prompt_figures_json(with_combined), _KNOWN)
    assert after.snapshot == "checked" and after.unverified_figures == []


# --- third re-verification 2026-09-21 (the audit author's M9-a) --------------------------

#: The six market caps the audit author's 13 regenerated cards printed, each with the
#: ``fundamentals_json.market_cap`` its prompt was fed (read from the demo's stored
#: ``prompt_figures``) and the line exactly as the card wrote it. Four are right and were
#: flagged only because the scale table had no 兆; two are ×10 unit errors — the same class
#: as #172 — and must stay flagged.
_MARKET_CAPS = [
    # (card, fed market_cap, the card's line, flagged token or None)
    ("#186 AAPL", "4514709672075.806", "*   市值 (Market Cap): 4.51 兆 USD", None),
    ("#187 MSFT", "3588320530555.747", "*   市值：3.59 兆 USD", None),
    ("#189 TSLA", "1433132709532.1418", "*   市值：USD 1.43 兆", None),
    (
        "#180 2330",
        "62497011861470",
        "*   **yfinance (2026-08-23):** 本益比 28.63，股價淨值比 11.67，近四季 EPS 84.19 TWD，"
        "市值 62.5 兆 TWD，殖利率 0.91%，股東權益報酬率 41.40%，營收年增率 36.05%。",
        None,
    ),
    ("#188 NVDA", "5200733149566.65", "*   市值達 5,200 億美元以上。", "5,200 億"),
    (
        "#185 8299",
        "458917375000",
        "*   **市值**：458.92 億 TWD (yfinance, 2026-08-23)",
        "458.92 億",
    ),
]


def test_trillion_market_caps_verify_and_the_x10_ones_stay_flagged() -> None:
    """M9-a: the scale table had 千 / 萬 / 億 and no 兆, so 「4.51 兆」 was read as the bare
    number 4.51 and could match nothing. Pinned on the audit author's own six cards: the
    four 兆 figures verify, the two ×10 億 figures are still caught."""
    from portfolio_dash.llm_insight.figure_check import prompt_figures_json

    for card, fed, line, flagged in _MARKET_CAPS:
        # the rest of #180's line is ratios the prompt also carried
        prompt = (
            f'{{"market_cap":{fed},"pe":28.63,"pb":11.67,"eps":84.19,'
            '"dividend_yield":"0.91%","roe":"41.40%","revenue_growth":"36.05%"}'
        )
        flags = check_figures(line, prompt_figures_json(prompt), _KNOWN)
        assert flags.snapshot == "checked", card
        assert flags.unverified_figures == ([flagged] if flagged else []), card


def test_compound_zh_units_multiply() -> None:
    """The class behind M9-a, not just its instance: a zh magnitude is a PREFIX (十 / 百 /
    千) times a BASE (萬 / 億 / 兆), and 萬億 is 兆. Measured on the demo's older cards:
    「已實現獲利 3.66 百萬 TWD」 read as the bare 3.66, 「市值約 1.38 千萬元」 read as 1,380."""
    snapshot = json.dumps(
        {"a": "3660000", "b": "13800000", "c": "113000000000", "d": "2500000000000",
         "e": "52000000000"}
    )
    text = "獲利 3.66 百萬，市值 1.38 千萬，營收 1.13 千億，總額 2.5 萬億，約 52 十億。"
    assert check_figures(text, snapshot, _KNOWN).unverified_figures == []
    # the same figures one unit off are still scale errors
    wrong = check_figures("獲利 3.66 千萬，市值 1.38 百萬", snapshot, _KNOWN)
    assert wrong.unverified_figures == ["3.66 千萬", "1.38 百萬"]


def test_hundred_alone_is_not_a_unit() -> None:
    """百 is only a prefix. Standing alone after a number it is 百分位 / 百分點 — a
    percentile or percentage points — and reading 「20 百分位」 as 2,000 or 「0.5 百分點」
    as 50 would paint a correct card 待核 (the demo has 「20百分位以下」 on a stored card)."""
    snapshot = json.dumps({"pctile": "20", "pp": "0.5"})
    text = "股價淨值比進入歷史低位（20百分位以下），利差擴大 0.5 百分點。"
    assert check_figures(text, snapshot, _KNOWN).unverified_figures == []


def test_a_mixed_zh_numeral_is_one_figure() -> None:
    """「市值突破 1兆1,300億元」 (a stored demo card) is ONE number, 1.13 兆 — read as two
    figures, 1 兆 and 1,300 億, it could match nothing a prompt ever held."""
    snapshot = json.dumps({"market_cap": "1131234000000", "cost": "350000000"})
    ok = check_figures("市值突破 1兆1,300億元，成本 3億5,000萬。", snapshot, _KNOWN)
    assert ok.unverified_figures == []
    bad = check_figures("市值突破 1兆3,100億元。", snapshot, _KNOWN)
    assert bad.unverified_figures == ["1兆3,100億"]
    # two SEPARATE figures are not merged just because both carry a unit
    apart = check_figures("市值 1兆，成本 1,300億。", snapshot, _KNOWN)
    assert apart.unverified_figures == ["1兆", "1,300億"]


def test_latin_billion_and_trillion_suffixes() -> None:
    """The audit author's suggestion: a model also writes 「4.51T」 / 「520B」. Upper-case
    only, and never inside a word or an ISO timestamp (「2026-07-06T00:12」 is on 12 cards)."""
    snapshot = json.dumps({"cap": "4514709672075.806", "rev": "520000000000"})
    assert check_figures("市值 4.51T USD，營收 520B。", snapshot, _KNOWN).unverified_figures == []
    assert check_figures("市值 4.51B USD", snapshot, _KNOWN).unverified_figures == ["4.51B"]
    stamp = check_figures("資料時間 2026-07-06T00:12:05，營收 520B。", snapshot, _KNOWN)
    assert stamp.unverified_figures == []


def test_the_prompt_side_reads_the_same_scale_grammar() -> None:
    """The population is extracted with the SAME grammar the card is read with, or a news
    line fed as 「營收 3.82 兆元」 would be stored as 3.82 and the card quoting it flagged."""
    from decimal import Decimal

    from portfolio_dash.llm_insight.figure_check import prompt_figures_json

    population = {
        Decimal(token)
        for token in json.loads(
            prompt_figures_json("新聞：營收 3.82 兆元；獲利 3.66 百萬；市值 1兆1,300億；4.51T")
        )
    }
    assert population == {
        Decimal("3820000000000"), Decimal("3660000"), Decimal("1130000000000"),
        Decimal("4510000000000"),
    }
