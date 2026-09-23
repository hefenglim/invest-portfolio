"""G-01: no frontend surface renders the API's raw ``accounts.name``.

Two spellings of one account coexisted in the app. ``web/names.js`` (FU-D37) is the
frontend's naming authority and says 嘉信 Schwab / 台灣券商 / Moomoo MY; the server's
``accounts.name`` column says Charles Schwab / TW Broker / Moomoo MY, and every ledger
row carries BOTH a stable ``account_id`` and that English ``account`` display string.
A surface that printed ``row.account`` therefore disagreed with the surface next to it —
measured 2026-09-02 inside ONE drawer: the filter chip read 「嘉信 Schwab」 and every row
it filtered read 「Charles Schwab」 in its own 帳戶 column, two tables apart.

``names.js``'s own preamble predicted this gap ("surfaces that render an account label
straight from the API payload"), but framed it as a per-PAGE split. It was not: it had
reached adjacent tables of the same drawer, which is what makes a comment an insufficient
guard and this file necessary.

The scan is deliberately about the READ, not about what is done with the value: reading
``.account`` off an API row is the drift signal, and the fix is always the same —
``acctZh(row.account_id)`` -> ``window.pdNames.account``. Local UI state that happens to
be named ``account`` holds an account *id*, never a name; those receivers are allowlisted
by name below.

⚠ ``_PENDING`` is NOT a permanent exemption — it is the list of files whose fix was out of
this change's scope. Fixing one only ever removes a name from it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# tests/contract/this_file.py -> parents[2] == worktree root (web/ lives here).
_WEB_DIR = Path(__file__).resolve().parents[2] / "web"

# `<ident>.account` or `<ident>.account_name` — a property read of the ENGLISH display name.
#
# ⚠ This pattern used to be `…\.account\b` alone, with a comment claiming `account_id` /
# `account_name` were "excluded by the pattern itself" as though both exclusions were
# correct. Excluding `account_id` is correct — it is an id, and resolving it is the fix.
# Excluding `account_name` was a HOLE: `/api/dashboard` sends
# `holdings[].account_name = "Charles Schwab"`, so it is the English display name under a
# second key. Regression R5 measured the consequence on a page this guard reported clean:
# `index.html` → 再平衡試算 printed 「Charles Schwab 129.1338股」 beside eighteen cells
# reading 「嘉信 Schwab」 — G-01's exact defect, on a surface declared fixed, with the guard
# green. A guard that certifies one spelling certifies one spelling.
#
# The account-LIST endpoints (`/api/input/context`, `/api/accounts`) carry the same English
# string as plain `.name`. `\w+\.name` cannot be flagged site-wide without drowning the scan
# in unrelated matches, so that spelling is guarded per FILE instead: every web/*.js that
# fetches one of those lists is found by `_account_list_readers()` and held to the rules in
# the last three tests. ⚠ This comment used to name `web/broker-import.js` as a KNOWN LIMIT
# "to be found by reading" while the test beside it listed cash.js and input.js by hand —
# and the second full re-verification (2026-09-22, M5-b) found broker-import.js rendering
# 「TW Broker（tw_broker）」. A limit that is written down but not enforced is a to-do list.
_RAW_ACCOUNT_READ = re.compile(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\.account(?:_name)?\b")

# Receivers that are NOT an API row:
#   pdNames  — the resolver itself (`window.pdNames.account(id)`), i.e. the fix.
#   state / holdingsState / stmt / opts — page filter state; the value is an account_id
#   ('all' or e.g. 'tw_broker'), which is why they are compared and passed as params,
#   never rendered.
_LOCAL_STATE_RECEIVERS = frozenset({"pdNames", "state", "holdingsState", "stmt", "opts"})

# Files whose raw reads are known and out of scope for the current fix. EMPTY since
# 2026-09-16 (demo audit M5): the six deferred surfaces — cash.js (statement / movement
# tables, edit-modal titles), corp-action-form.js (preview cards), input.js (the CSV
# preview's 帳戶 column and both account <select>s), rebalance.js (per-account chips and
# legs), rebate-inbox.js (折讓款 titles) and inbox.js (配息 titles) — all resolve through
# window.pdNames now, and every page hosting them loads names.js. The set stays so a
# future deferral has a home, and `test_the_pending_list_is_not_stale` keeps it honest.
_PENDING: frozenset[str] = frozenset()

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"//[^\n]*")


def _strip_js_comments(src: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", src))


def _violations(src: str) -> list[str]:
    """Every raw account-display-name read in *src* (comments already stripped)."""
    return [m.group(0) for m in _RAW_ACCOUNT_READ.finditer(src)
            if m.group(1) not in _LOCAL_STATE_RECEIVERS]


def _scan(path: Path) -> list[str]:
    return _violations(_strip_js_comments(path.read_text(encoding="utf-8")))


def _web_js() -> list[Path]:
    return [p for p in sorted(_WEB_DIR.glob("*.js")) if p.name != "echarts.min.js"]


def test_the_detector_can_see_a_violation() -> None:
    """Positive control: without it, an always-empty scan would 'pass' forever."""
    assert _violations("tr.appendChild(el('td', 'col-text', t.account));") == ["t.account"]
    # and the allowlisted forms are genuinely not flagged
    assert _violations("window.pdNames.account(id); if (state.account === 'all') {}") == []
    assert _violations("h.account_id; a.accountShort(x);") == []
    # The second spelling, the one this guard was blind to until regression R5 measured it
    # rendering 「Charles Schwab」 on an already-"fixed" page.
    assert _violations("chip.textContent = r.account_name;") == ["r.account_name"]
    assert _violations("if (state.account_name) {}") == []  # local state still exempt


def test_names_js_is_the_single_frontend_naming_authority() -> None:
    """The resolver the fix delegates to must exist (else every call site silently
    degrades to the raw id and this guard would pass over a broken app)."""
    src = (_WEB_DIR / "names.js").read_text(encoding="utf-8")
    assert "window.pdNames" in src
    for zh in ("台灣券商", "嘉信 Schwab", "Moomoo MY"):
        assert zh in src, f"names.js lost the canonical display name {zh!r}"


@pytest.mark.parametrize("name", ["app.js", "detail.js", "ledger.js"])
def test_dashboard_ledger_and_drawer_never_render_a_raw_account_name(name: str) -> None:
    """The three files that own account naming via names.js: exactly zero raw reads.

    These are the surfaces the 2026-09-02 sweep measured — 各帳戶現金, the drawer's
    交易明細, and the six 交易帳本 tables — and they sit beside chips/columns that already
    resolve through ``acctZh``. Nothing here may reintroduce the second spelling.
    """
    found = _scan(_WEB_DIR / name)
    assert not found, (
        f"web/{name} renders the API's English accounts.name: {found}. "
        "Use acctZh(row.account_id) (window.pdNames) — see web/names.js."
    )


def test_no_new_surface_reads_a_raw_account_name() -> None:
    """Whole-web sweep: a NEW table anywhere fails here, which is the point.

    Only ``_PENDING`` is tolerated, and only as a subset — a file that gets fixed simply
    stops appearing, so this never blocks the cleanup it exists to drive.
    """
    offenders = {p.name: found for p in _web_js()
                 if p.name not in _PENDING and (found := _scan(p))}
    assert not offenders, (
        "new surface(s) rendering the raw API account name: "
        + "; ".join(f"{k}: {v}" for k, v in offenders.items())
        + " — resolve through window.pdNames.account(row.account_id)."
    )


def test_the_pending_list_is_not_stale() -> None:
    """An allowlist nobody uses is an allowlist nobody notices (D39, applied here).

    Every ``_PENDING`` entry must still exist AND still violate; once its fix lands, this
    fails and the name must be deleted from the list rather than left as cover.
    """
    for name in sorted(_PENDING):
        path = _WEB_DIR / name
        if not path.exists():
            pytest.fail(f"_PENDING lists web/{name}, which no longer exists — drop it.")
        assert _scan(path), (
            f"web/{name} no longer renders a raw account name — remove it from _PENDING "
            "so the file is guarded like the rest."
        )


#: G-01's SECOND blind spot (DEF-035, 2026-09-23). The scan above flags reads of the English
#: display NAME and deliberately passes `.account_id` — resolving the id IS the fix. Nothing
#: checked that it WAS resolved before reaching the screen: the AI draft tables printed
#: `el('td', 'col-text', d.account_id || '')` three times, so the verifier read 「tw_broker」
#: in a table directly above a CSV preview reading 「台灣券商」, with this file green. A raw id
#: RENDERED — as an `el(...)` text argument, a `textContent`, or glued into a sentence — is the
#: same defect as a raw name; a line that resolves it through pdNames / acctZh is not flagged.
_RAW_ID_RENDER = re.compile(
    r"\bel\([^;\n]*?,\s*(?:[\w$]+\.)+account_id\s*(?:\|\|\s*'[^']*')?\s*\)"
    r"|\.(?:textContent|innerText)\s*=\s*[^;\n]*?\b(?:[\w$]+\.)+account_id\b"
    r"|['`][^'`\n]*['`]\s*\+\s*(?:[\w$]+\.)+account_id\b"
    r"|\b(?:[\w$]+\.)+account_id\s*\+\s*['`]"
)
_RESOLVED = re.compile(r"pdNames|acctZh\(|accountName\(")
#: Known raw-id renders OUTSIDE the current change's scope, with the reason. Shrinks only.
_ID_PENDING: dict[str, str] = {}


def _raw_id_renders(src: str) -> list[str]:
    return [line.strip() for line in _strip_js_comments(src).splitlines()
            if _RAW_ID_RENDER.search(line) and not _RESOLVED.search(line)]


def test_the_raw_id_detector_sees_the_measured_lines() -> None:
    """Positive control: the three DEF-035 lines, verbatim, and their fixed forms."""
    for measured in ("tr.appendChild(el('td', 'col-text', d.account_id || ''));",
                     "cell.textContent = row.account_id;",
                     "title = '帳戶 ' + r.account_id;"):
        assert _raw_id_renders(measured), measured
    for fixed in ("tr.appendChild(el('td', 'col-text', acctZh(d.account_id)));",
                  "o.value = a.account_id;",
                  "loadAcctHoldings(d.account_id, false);"):
        assert not _raw_id_renders(fixed), fixed


def test_no_surface_renders_a_raw_account_id() -> None:
    offenders = {p.name: found for p in _web_js()
                 if p.name not in _ID_PENDING
                 and (found := _raw_id_renders(p.read_text(encoding="utf-8")))}
    assert not offenders, (
        "raw account id(s) rendered to the screen: "
        + "; ".join(f"{k}: {v}" for k, v in offenders.items())
        + " — resolve through window.pdNames.account(id)."
    )


def test_the_id_pending_list_is_not_stale() -> None:
    for name in _ID_PENDING:
        path = _WEB_DIR / name
        assert path.exists(), f"_ID_PENDING lists web/{name}, which no longer exists"
        assert _raw_id_renders(path.read_text(encoding="utf-8")), (
            f"web/{name} no longer renders a raw account id — remove it from _ID_PENDING")


#: The two endpoints that list accounts with the English `name`, as a page fetches them.
_ACCOUNT_LIST_FETCH = re.compile(r"""['"`]/api/(?:input/context|accounts)['"`]""")
#: `symCell(x.symbol, x.name)` — an instrument's code + name cell, whatever the row is called.
_INSTRUMENT_NAME_CELL = re.compile(r"symCell\(\s*(\w+)\.symbol\s*,\s*\1\.name\s*\)")


def _account_list_readers() -> dict[str, str]:
    """Every web/*.js that fetches an account list, with its comment-stripped source.

    DISCOVERED, never listed by hand: the hand-written ("cash.js", "input.js") this replaced
    is exactly how broker-import.js stayed out of reach (M5-b, 2026-09-22).
    """
    out = {}
    for path in _web_js():
        src = _strip_js_comments(path.read_text(encoding="utf-8"))
        if _ACCOUNT_LIST_FETCH.search(src):
            out[path.name] = src
    return out


def test_the_account_list_readers_are_discovered() -> None:
    """Positive control: an empty discovery would pass the two tests below forever. The
    five files known to fetch an account list today must all be found."""
    found = set(_account_list_readers())
    expected = {"broker-import.js", "cash.js", "corp-action-form.js", "input.js", "ledger.js"}
    assert expected <= found, f"discovery lost {sorted(expected - found)}"


def test_the_context_accounts_english_name_is_never_rendered_either() -> None:
    """The second spelling has a SECOND source the `.account` scanner cannot see.

    `/api/input/context` lists accounts as `{id, name, …}` with the English `name`, and
    cash.js rendered `a.name` as the 各帳戶現金 card header and in its account <select>s —
    measured on the deployed demo on 2026-09-16 AFTER the six `_PENDING` files had been
    fixed: the cards still read 「TW Broker」. broker-import.js did the same in its 匯入到帳戶
    select (M5-b, 2026-09-22), in a file this test did not open. Any `a.name` in a file that
    fetches an account list is that defect; the resolver takes `a.id`.
    """
    for name, src in _account_list_readers().items():
        # A ledger ROW named `a` also has a `.name` — the INSTRUMENT's (ledger.js's 公司行動
        # table: `symCell(a.symbol, a.name)`). That pairing is structurally a symbol cell,
        # never an account label, so it is removed before the scan rather than excused.
        src = _INSTRUMENT_NAME_CELL.sub("", src)
        assert re.search(r"\ba\.name\b", src) is None, (
            f"web/{name} renders the account list's English name — use pdNames.account(a.id)"
        )


def test_every_broker_adapter_has_a_display_name() -> None:
    """M5-b's second half: the 券商對帳單 picker hard-coded 「Charles Schwab」 and fell back to
    the raw id for any other adapter. Its labels now come from `pdNames.broker`, so a new
    adapter in the registry without a names.js entry would reach the page as a bare id."""
    from portfolio_dash.data_ingestion.broker.registry import BROKER_IDS

    names = (_WEB_DIR / "names.js").read_text(encoding="utf-8")
    table = re.search(r"const BROKERS = \{(.*?)\};", names, flags=re.S)
    assert table, "names.js lost the BROKERS table"
    keys = set(re.findall(r"^\s*([a-z_]+)\s*:", table.group(1), flags=re.M))
    assert set(BROKER_IDS) <= keys, f"no zh name for broker(s) {sorted(set(BROKER_IDS) - keys)}"
    src = _strip_js_comments((_WEB_DIR / "broker-import.js").read_text(encoding="utf-8"))
    assert "names.broker(" in src and "'Charles Schwab'" not in src


def test_every_account_option_label_comes_from_the_one_definition() -> None:
    """L8 (demo audit 2026-09-16), re-verified 2026-09-17 as PARTIAL: the class, not the instance.

    The audit's title named 「交易輸入／股利／換匯 帳戶下拉」. The fix derived the label
    (zh name + the currencies the account TRADES in, from its bound markets) inside
    input.js, so its three selects read 「Moomoo MY（USD／MYR）」 — and cash.js's 換匯 and
    出金入金 selects, which build their options from the same context list, kept bracketing
    the SETTLEMENT currency: 「Moomoo MY（USD）」. One derivation now lives in names.js as
    `pdNames.accountOption`; this pins that every file building an account <option> calls
    it and that no file derives the bracket from `settlement_ccy` / `ccy` on its own.
    """
    names = (_WEB_DIR / "names.js").read_text(encoding="utf-8")
    assert "accountOption(a)" in names, "names.js lost the account <option> label authority"
    assert "MARKET_CCY" in names  # the bracket is the TRADING currencies, market-derived

    # Every file that builds <option>s from the CONTEXT list (discovered, M5-b — the tuple
    # this replaced named cash.js and input.js and missed broker-import.js).
    builders = {name: src for name, src in _account_list_readers().items()
                if "/api/input/context" in src and "'option'" in src}
    assert {"broker-import.js", "cash.js", "input.js"} <= set(builders), sorted(builders)
    for name, src in builders.items():
        assert re.search(r"\.accountOption\(", src), (
            f"web/{name} builds an account <option> without pdNames.accountOption"
        )
        # The settlement-ccy bracket may survive ONLY as the no-names.js fallback on the
        # same expression that prefers the authority — never as a label of its own.
        for m in re.finditer(r"'（'\s*\+\s*settlementCcy\(a\)", src):
            window = src[max(0, m.start() - 200):m.start()]
            assert "pdNames.accountOption" in window, (
                f"web/{name}: a settlement-currency option label that is not the fallback "
                "of pdNames.accountOption"
            )
        # …and the market→currency table exists in exactly one place.
        assert "_MARKET_CCY" not in src, f"web/{name} re-derives the market→currency table"
