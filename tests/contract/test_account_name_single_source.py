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
# ⚠ KNOWN LIMIT, deliberately not closed here: `/api/accounts` rows carry the same English
# string as plain `.name` (`web/broker-import.js`, and the 帳戶 dropdowns in `web/input.js`).
# `\w+\.name` cannot be flagged without drowning the scan in unrelated matches, so those
# sites are invisible to this test and must be found by reading. Written down rather than
# left implicit: the failure mode being guarded against is exactly "nobody remembered".
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


def test_the_context_accounts_english_name_is_never_rendered_either() -> None:
    """The second spelling has a SECOND source the `.account` scanner cannot see.

    `/api/input/context` lists accounts as `{id, name, …}` with the English `name`, and
    cash.js rendered `a.name` as the 各帳戶現金 card header and in its account <select>s —
    measured on the deployed demo on 2026-09-16 AFTER the six `_PENDING` files had been
    fixed: the cards still read 「TW Broker」. Any `a.name` in the files that iterate that
    list is that defect; the resolver takes `a.id`.
    """
    for name in ("cash.js", "input.js"):
        src = _strip_js_comments((_WEB_DIR / name).read_text(encoding="utf-8"))
        assert re.search(r"\ba\.name\b", src) is None, (
            f"web/{name} renders the context accounts' English name — use acctZh(a.id)"
        )


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

    for name in ("cash.js", "input.js"):
        src = _strip_js_comments((_WEB_DIR / name).read_text(encoding="utf-8"))
        assert "pdNames.accountOption(" in src, (
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
