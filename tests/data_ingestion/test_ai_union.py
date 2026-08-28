"""W4 (AI-D17/D18/D19/D21): the AI door's discriminated union, end to end.

The pins that matter, each tied to the failure it refuses to re-admit:

* the three per-kind CSVs re-parse through the REAL whole-file builders with zero
  ``parse_error`` — the same guard the downloadable templates get
  (``tests/contract/test_import_template.py``), because an AI CSV that its own door
  cannot read is the AI-1/AI-2 class all over again;
* ``short_sale`` rides preview → CSV → commit → ledger (the W1 daytrade disproof,
  applied to the flag that arrived with its prompt rule in v6);
* the cash pool probe is a REQUIRED argument — forgetting it is a TypeError, never a
  quietly weaker guard (``cash_import.py``'s module docstring is the authority);
* a dividend row missing ``gross`` fails at the PARSE boundary, not inside a builder
  (AI-D17 — the discriminator's whole point);
* a zh cash-kind alias (入金) canonicalises at the SAME door the CSV kind uses, so the
  two doors cannot drift on what 入金 means.
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from portfolio_dash.data_ingestion.agents import (
    AiDraftList,
    CashDraft,
    Completer,
    DivDraft,
    TxnDraft,
    UnparsedRow,
    _cash_csv,
    ai_agents_input,
)
from portfolio_dash.data_ingestion.cash_import import build_cash_movement_preview
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.csv_import import (
    build_transaction_preview,
    write_transaction_row,
)
from portfolio_dash.data_ingestion.dividend_import import build_dividend_preview
from portfolio_dash.data_ingestion.preview import commit_preview
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.data_ingestion.validate import CashPool
from portfolio_dash.shared import llm_fail_log as fail_log
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side


def _RICH_POOL(account_id: str, ccy: Currency, **kw: object) -> CashPool:
    """Effectively infinite headroom — the withdraw guard never fires."""
    return CashPool(balance=Decimal("999999999"), low=Decimal("999999999"))


def _setup(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(
        conn,
        Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                   sector="Tech", name="台積電"),
    )
    upsert_instrument(
        conn,
        Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                   sector="Tech", name="Apple"),
    )


def _union_completer(
    txns: list[TxnDraft] | None = None,
    divs: list[DivDraft] | None = None,
    cashs: list[CashDraft] | None = None,
    unparsed: list[UnparsedRow] | None = None,
) -> Completer:
    rows: list[object] = [*(txns or []), *(divs or []), *(cashs or [])]

    def _c(
        prompt: str, schema: type, *, agent: str, conn: object = None,
        images: list[bytes] | None = None, model_override: str | None = None,
    ) -> AiDraftList:
        return AiDraftList.model_validate({"rows": rows, "unparsed": unparsed or []})

    return _c


# ------------------------------------------------------------------ round-trip guard


def test_every_kind_csv_reparses_through_its_real_builder(conn: sqlite3.Connection) -> None:
    """The AI door's three CSVs through the three REAL doors: zero parse_error rows.

    This is the import-template round-trip guard applied to the union — the AI CSVs are
    byte-shaped by the same column constants the templates are generated from, and this
    test is what keeps that claim honest.
    """
    _setup(conn)
    result = ai_agents_input(
        conn, "mixed",
        pool=_RICH_POOL,
        completer=_union_completer(
            txns=[TxnDraft(account_id="tw_broker", symbol="2330", side=Side.BUY,
                           date=date(2026, 6, 1), shares=Decimal("1000"),
                           price=Decimal("600"))],
            divs=[DivDraft(account_id="tw_broker", symbol="2330", date=date(2026, 6, 3),
                           type="CASH", gross=Decimal("1000"))],
            cashs=[CashDraft(account_id="tw_broker", date=date(2026, 6, 1),
                             cash_kind="DEPOSIT", ccy="TWD", amount=Decimal("50000"))],
        ),
    )
    assert set(result.previews) == {"transactions", "dividends", "cash"}

    txn_re = build_transaction_preview(conn, result.csv_texts["transactions"])
    div_re = build_dividend_preview(conn, result.csv_texts["dividends"])
    cash_re = build_cash_movement_preview(conn, result.csv_texts["cash"], pool=_RICH_POOL)
    for rederived in (txn_re, div_re, cash_re):
        assert [r.index for r in rederived.rows
                if any(i.kind == "parse_error" for i in r.issues)] == []
    # And the re-derived rows answer the SAME numbers the AI preview showed (AI-D18's
    # by-construction claim, asserted once at the seam).
    assert txn_re.rows[0].fee == result.previews["transactions"].rows[0].fee
    assert (div_re.rows[0].payload["gross"]
            == result.previews["dividends"].rows[0].payload["gross"])
    assert (cash_re.rows[0].payload["amount"]
            == result.previews["cash"].rows[0].payload["amount"])


def test_a_kind_with_zero_drafts_is_absent_not_empty(conn: sqlite3.Connection) -> None:
    """Empty sections are the frontend's cue to not render — the backend omits the key."""
    _setup(conn)
    result = ai_agents_input(
        conn, "buy only", pool=_RICH_POOL,
        completer=_union_completer(txns=[TxnDraft(
            account_id="tw_broker", symbol="2330", side=Side.BUY,
            date=date(2026, 6, 1), shares=Decimal("10"), price=Decimal("600"))]),
    )
    assert set(result.previews) == {"transactions"}
    assert set(result.csv_texts) == {"transactions"}


# ------------------------------------------------------------------ pool injection


def test_the_pool_probe_is_required(conn: sqlite3.Connection) -> None:
    """Forgetting the bind is a TypeError, never a quietly weaker guard (AI-D18)."""
    _setup(conn)
    with pytest.raises(TypeError):
        ai_agents_input(  # type: ignore[call-arg]  # the missing arg IS the test
            conn, "入金 50000", completer=_union_completer())


def test_zh_cash_kind_alias_canonicalises_at_the_same_door(conn: sqlite3.Connection) -> None:
    """入金 → DEPOSIT through the CSV kind's OWN alias table — one vocabulary, two doors."""
    _setup(conn)
    result = ai_agents_input(
        conn, "入金", pool=_RICH_POOL,
        completer=_union_completer(cashs=[CashDraft(
            account_id="tw_broker", date=date(2026, 6, 1), cash_kind="入金",
            ccy="TWD", amount=Decimal("50000"))]),
    )
    row = result.previews["cash"].rows[0]
    assert row.payload["kind"] == "DEPOSIT"
    # AI-D21: the server-owned label + explicit sign ride the payload.
    assert row.payload["kind_label"] == "入金"
    assert row.payload["sign"] == "1"


def test_cash_row_sign_is_negative_for_a_debit(conn: sqlite3.Connection) -> None:
    """券商費用 is an OUTFLOW — the sign must say so before any number is read."""
    _setup(conn)
    result = ai_agents_input(
        conn, "券商費用", pool=_RICH_POOL,
        completer=_union_completer(cashs=[CashDraft(
            account_id="schwab", date=date(2026, 6, 1), cash_kind="券商費用",
            ccy="USD", amount=Decimal("25"))]),
    )
    row = result.previews["cash"].rows[0]
    assert row.payload["kind"] == "BROKER_FEE"
    assert row.payload["kind_label"] == "券商費用"
    assert row.payload["sign"] == "-1"


def test_acq_home_amount_rides_when_the_statement_stated_it(conn: sqlite3.Connection) -> None:
    """F1's column reaches the payload — the AMOUNT, never a rate (AI-D19's strict rule)."""
    _setup(conn)
    result = ai_agents_input(
        conn, "入金 1000 USD 成本 31500 TWD", pool=_RICH_POOL,
        completer=_union_completer(cashs=[CashDraft(
            account_id="schwab", date=date(2026, 6, 1), cash_kind="DEPOSIT",
            ccy="USD", amount=Decimal("1000"), acq_home_amount=Decimal("31500"))]),
    )
    row = result.previews["cash"].rows[0]
    assert not row.has_hard_issue
    assert row.payload["acq_home_amount"] == "31500"


# ------------------------------------------------------------------ short_sale (AI-D19)


def test_short_sale_reaches_the_ledger_not_just_the_preview(conn: sqlite3.Connection) -> None:
    """The W1 daytrade disproof, applied to the flag v6 admitted.

    A declared short is exempt from the 賣超 guard at the preview — but the commit
    re-derives from the CSV, so if the flag did not ride the CSV the ledger would book
    a bare oversell instead of a short position: the DANGEROUS direction (a wrong number
    that looks right), and exactly why the column and its prompt rule ship together.
    """
    _setup(conn)
    result = ai_agents_input(
        conn, "放空 2330 100 股 @600", pool=_RICH_POOL,
        completer=_union_completer(txns=[TxnDraft(
            account_id="tw_broker", symbol="2330", side=Side.SELL,
            date=date(2026, 6, 1), shares=Decimal("100"), price=Decimal("600"),
            short_sale=True)]),
    )
    row = result.previews["transactions"].rows[0]
    assert row.payload["short_sale"] == "1"
    assert not any(i.kind == "sell_exceeds_holdings" for i in row.issues)

    rederived = build_transaction_preview(conn, result.csv_texts["transactions"])
    assert rederived.rows[0].payload["short_sale"] == "1"
    summary = commit_preview(conn, rederived, accept={0}, writer=write_transaction_row)
    assert len(summary.written) == 1
    stored = conn.execute("SELECT short_sale FROM transactions").fetchone()
    assert stored is not None and stored[0] == 1


def test_an_undeclared_oversell_still_flags(conn: sqlite3.Connection) -> None:
    """The guard did not go soft: the SAME sell without the flag still warns 賣超."""
    _setup(conn)
    result = ai_agents_input(
        conn, "賣出 2330 100 股 @600", pool=_RICH_POOL,
        completer=_union_completer(txns=[TxnDraft(
            account_id="tw_broker", symbol="2330", side=Side.SELL,
            date=date(2026, 6, 1), shares=Decimal("100"), price=Decimal("600"))]),
    )
    row = result.previews["transactions"].rows[0]
    assert any(i.kind == "sell_exceeds_holdings" for i in row.issues)


# ------------------------------------------------------------------ sibling awareness


def test_the_txn_arm_is_sibling_aware(conn: sqlite3.Connection) -> None:
    """C1 reaches this door: a sell covered by a buy EARLIER in the same paste is clean."""
    _setup(conn)
    result = ai_agents_input(
        conn, "6/1 買 10 股，6/2 賣 10 股", pool=_RICH_POOL,
        completer=_union_completer(txns=[
            TxnDraft(account_id="tw_broker", symbol="2330", side=Side.BUY,
                     date=date(2026, 6, 1), shares=Decimal("10"), price=Decimal("600")),
            TxnDraft(account_id="tw_broker", symbol="2330", side=Side.SELL,
                     date=date(2026, 6, 2), shares=Decimal("10"), price=Decimal("610")),
        ]),
    )
    sell = result.previews["transactions"].rows[1]
    assert not any(i.kind == "sell_exceeds_holdings" for i in sell.issues)


# ------------------------------------------------------------------ the parse boundary


def test_a_dividend_row_missing_gross_fails_at_the_parse_boundary() -> None:
    """AI-D17: the discriminator rejects a malformed row BEFORE any builder sees it."""
    with pytest.raises(ValidationError):
        AiDraftList.model_validate({"rows": [{
            "kind": "div", "account_id": "tw_broker", "symbol": "2330",
            "date": "2026-06-03", "type": "CASH",  # no gross
        }], "unparsed": []})


def test_a_mistyped_optional_field_fails_at_the_parse_boundary() -> None:
    """``with_hold`` is NOT ``withholding`` — the same boundary, from the other side.

    Pydantic's default IGNORES unknown fields, so a mistyped optional money field used to
    parse clean with the value gone: the dividend model then computes its own withholding
    where the statement's number was read and dropped. ``extra="forbid"`` turns the typo
    into the same parse-boundary rejection (and retry) a missing required field gets —
    silent information loss is AI-D3's sin whichever direction the field went missing.
    """
    with pytest.raises(ValidationError):
        AiDraftList.model_validate({"rows": [{
            "kind": "div", "account_id": "schwab", "symbol": "AAPL",
            "date": "2026-06-01", "type": "DRIP", "gross": "105",
            "with_hold": "31.5",  # typo for withholding
        }], "unparsed": []})
    with pytest.raises(ValidationError):
        AiDraftList.model_validate({"rows": [{
            "kind": "cash", "account_id": "schwab", "date": "2026-06-01",
            "cash_kind": "DEPOSIT", "ccy": "USD", "amount": "1000",
            "acq_home_amt": "31500",  # typo for acq_home_amount — the F1 cost, silently lost
        }], "unparsed": []})


def test_the_v5_drafts_key_fails_loud_not_empty() -> None:
    """A model regressing to the v5 shape must NOT parse to an empty extraction.

    ``{"drafts": [...]}`` under an ignore-extras policy is ``rows=[]`` — every extracted
    row silently dropped and an empty preview shown as if the statement held nothing.
    """
    with pytest.raises(ValidationError):
        AiDraftList.model_validate({"drafts": [{
            "kind": "txn", "account_id": "tw_broker", "symbol": "2330", "side": "BUY",
            "date": "2026-06-01", "shares": "10", "price": "600",
        }], "unparsed": []})


def test_an_unknown_kind_is_rejected_not_threaded_through() -> None:
    """``kind: "fx"`` is not a union member — it must not silently become anything."""
    with pytest.raises(ValidationError):
        AiDraftList.model_validate({"rows": [{
            "kind": "fx", "account_id": "schwab", "date": "2026-06-01",
        }], "unparsed": []})


def test_unparsed_rows_ride_through_untouched(conn: sqlite3.Connection) -> None:
    """The confession list reaches the result verbatim (AI-D17 — never a silent drop)."""
    _setup(conn)
    result = ai_agents_input(
        conn, "mixed", pool=_RICH_POOL,
        completer=_union_completer(
            unparsed=[UnparsedRow(text="AAPL 1拆4", reason="公司行動請用表單")]),
    )
    assert result.previews == {}
    assert [(u.text, u.reason) for u in result.unparsed] == [
        ("AAPL 1拆4", "公司行動請用表單")]


# ------------------------------------------------------------------ C7, per kind


def test_cash_csv_sanitizes_embedded_newlines_in_note() -> None:
    """C7 holds per kind, not only for transactions: a cash note carrying CR/LF must not
    split the draft across two CSV lines — else cash preview row ``n`` no longer maps to
    cash data line ``n + 1`` when the frontend commits the checked subset. The txn arm's
    pin lives in ``test_agents.py``; the helper is shared, but only a per-kind test keeps
    a future renderer from dropping the collapse for one kind."""
    csv = _cash_csv([CashDraft(account_id="tw_broker", date=date(2026, 1, 2),
                               cash_kind="DEPOSIT", ccy="TWD", amount=Decimal("1000"),
                               note="line1\nline2\r\nline3\rline4")])
    lines = csv.rstrip("\n").split("\n")
    assert len(lines) == 2  # header + exactly one data line
    assert "\r" not in csv
    assert lines[1].endswith("line1 line2 line3 line4")


def test_confessed_unparsed_rows_are_recorded_once(conn: sqlite3.Connection) -> None:
    """AI-D65: a successful call whose model confessed is still worth keeping.

    The seam records only calls that FAIL outright, so without this the confessions are
    rendered on screen once and thrown away -- and they are the richest fine-tuning
    signal, because the model is naming its own boundary against real text.

    ONE row per call, not per confessed line: the unit of diagnosis is "this paste, this
    prompt, this reply", and splitting it would duplicate the prompt N times while losing
    which confessions arrived together.
    """
    _setup(conn)
    fail_log.ensure_table(conn)
    completer = _union_completer(unparsed=[
        UnparsedRow(text="8/2 TSLA call 權利金 300", reason="options unsupported"),
        UnparsedRow(text="8/3 TWD→USD 31500", reason="fx conversion unsupported"),
    ])
    ai_agents_input(conn, "亂碼 ###", pool=_RICH_POOL, completer=completer)

    rows = fail_log.list_rows(conn)
    assert len(rows) == 1, "one row per CALL, not per confessed line"
    assert rows[0]["outcome"] == "unparsed_rows"
    assert rows[0]["agent"] == "ai_agents_input"
    assert "亂碼 ###" in str(rows[0]["source_text"])
    assert "TSLA" in str(rows[0]["raw_output"])
    assert "fx conversion unsupported" in str(rows[0]["raw_output"])
    assert "2 row(s)" in str(rows[0]["error_reason"])
    # A1: the confession row must carry its prompt too. Live on demo a
    # `schema_mismatch` row held 13,242 chars of prompt and an `unparsed_rows` row
    # held zero -- one corpus, two completenesses, the richest rows the poorer half.
    assert len(str(rows[0]["prompt"])) > 500
    assert "<task>" in str(rows[0]["prompt"])


def test_a_clean_extraction_records_nothing(conn: sqlite3.Connection) -> None:
    """The log must stay a FAILURE log -- a clean run leaves no trace."""
    _setup(conn)
    fail_log.ensure_table(conn)
    ai_agents_input(conn, "在元大買 10 股 2330 @ 600", pool=_RICH_POOL,
                    completer=_union_completer())
    assert fail_log.list_rows(conn) == []
