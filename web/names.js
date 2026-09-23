/* portfolio-dash — frontend account display-name resolver (FU-D37).

   SINGLE SOURCE OF TRUTH for how the frontend renders an account ID as a zh-TW
   display name. It replaces the three drifting per-file `ACCOUNT_ZH` maps that used
   to live in app.js / detail.js / ledger.js (they were identical copies that could —
   and did — drift out of sync). Every frontend surface that owns its OWN account
   naming now delegates here, so there is exactly one place to change a name.

   Canonical names follow the most-used existing zh convention (they are byte-identical
   to the strings the old maps carried — nothing is invented here). The account id is
   the stable key; `accountShort` offers a compact variant for tight chips, and the id
   itself is always available at the call site as the secondary/disambiguating form.

   ⚠ This paragraph used to read "NOT covered here (by design): surfaces that render an
   account label straight from the API payload". That exemption was framed as a per-PAGE
   split, and it was not one: by 2026-09-02 it had reached ADJACENT TABLES OF ONE DRAWER —
   a filter chip reading 「嘉信 Schwab」 above rows whose 帳戶 column read 「Charles Schwab」.
   G-01 therefore moved the dashboard's 各帳戶現金 card, the drawer's 交易明細 table and the
   six 交易帳本 tables onto this resolver (every ledger row already carries `account_id`), and
   `tests/contract/test_account_name_single_source.py` now fails on any web/*.js that renders
   the payload's raw `account` — so a new table cannot silently reopen the split. The few
   remaining API-fed surfaces (cash.js / corp-action-form.js / input.js) are named in that
   test's `_PENDING` list, which only ever shrinks.

   The PLANNED SUCCESSOR is unchanged and still owner-gated: a server-side
   `account.display_name` carried on /api/*, deferred to a future golden-payload re-baseline.
   Until then the names below are hard-coded here, so renaming an account in the DB does NOT
   change what the frontend shows.

   Load this BEFORE any dependent script (app.js / detail.js / ledger.js). Dependents
   degrade gracefully (id fallback, no crash) if it is absent. */
(function () {
  'use strict';

  /* id -> { name: full canonical zh, short: compact zh for chips }. The three
     first-class, config-seeded accounts after the Batch B merge (CLAUDE.md — account is
     a first-class entity), plus two RETAINED legacy ids (see below). An unknown id falls
     through to the id itself (see the resolver below). */
  const ACCOUNTS = {
    tw_broker:    { name: '台灣券商',    short: '台灣券商' },
    schwab:       { name: '嘉信 Schwab', short: '嘉信' },
    moomoo_my:    { name: 'Moomoo MY',   short: 'Moomoo MY' },
    /* Legacy pre-merge ids (Batch B merged moomoo_my_us + moomoo_my_my into moomoo_my).
       Retained so any pre-migration data snapshot still resolves to a name; harmless dict
       leftovers that T10's migration release may drop. */
    moomoo_my_us: { name: 'Moomoo 美股', short: 'Moomoo 美股' },
    moomoo_my_my: { name: 'Moomoo 馬股', short: 'Moomoo 馬股' }
  };

  /* Broker (statement FORMAT) id -> zh display name, for the 券商對帳單 picker. A broker is
     not an account, but the one that exists is the same company as the `schwab` account, so
     it carries the same spelling: the second re-verification of 2026-09-22 (M5-b) found
     broker-import.js hard-coding 「Charles Schwab」 in this picker directly above an account
     select reading 「TW Broker（tw_broker）」. Every id in
     `data_ingestion/broker/registry.py::BROKER_IDS` needs an entry here — pinned by
     tests/contract/test_account_name_single_source.py. */
  const BROKERS = {
    schwab: '嘉信 Schwab'
  };

  const asId = (id) => (id === null || id === undefined ? '' : String(id));

  /* Account REFERENCE token (DEF-023, 2026-09-23). The backend has no zh account name, so
     every user-visible backend sentence that names an account embeds `{account:<id>}`
     (portfolio_dash/shared/account_ref.py) and THIS resolver turns it into the display
     name — through api.js, which walks every response, so no page does it by hand. The
     grammar is pinned on both sides by tests/contract/test_account_ref_seam.py. */
  const ACCOUNT_REF = /\{account:([^{}\s]+)\}/g;

  /* The currency each market trades in — for the <option> label below. */
  const MARKET_CCY = { TW: 'TWD', US: 'USD', MY: 'MYR' };

  window.pdNames = {
    /* Full canonical zh display name for an account id (unknown id -> the id itself). */
    account(id) {
      const a = ACCOUNTS[id];
      return a ? a.name : asId(id);
    },
    /* Compact zh variant for space-constrained chips (unknown id -> the id itself). */
    accountShort(id) {
      const a = ACCOUNTS[id];
      return a ? a.short : asId(id);
    },
    /* zh display name for a broker-statement adapter id (unknown id -> the id itself). */
    broker(id) {
      return Object.prototype.hasOwnProperty.call(BROKERS, id) ? BROKERS[id] : asId(id);
    },
    /* Replace every `{account:<id>}` token in `text` with the account's display name.
       Non-strings and strings without a token come back untouched (same object). */
    resolveRefs(text) {
      if (typeof text !== 'string' || text.indexOf('{account:') === -1) return text;
      return text.replace(ACCOUNT_REF, (m, id) => window.pdNames.account(id));
    },
    /* Account <option> label for a `/api/input/context` account row `{id, ccy,
       settlement_ccy, markets}`: the zh name + the currencies the account actually TRADES
       in, derived from its bound markets (「Moomoo MY（USD／MYR）」, 「台灣券商（TWD）」).
       Demo audit 2026-09-16 L8 fixed this in input.js (交易輸入／股利／期初庫存); the
       re-verification of 2026-09-17 found cash.js still bracketing the SETTLEMENT currency
       (「Moomoo MY（USD）」 on 換匯／出金入金) — the instance was fixed, the class was not.
       One definition here, every account <select> calls it; the settlement/legacy `ccy`
       is only the fallback for a stale context lacking `markets`. */
    accountOption(a) {
      const name = a ? window.pdNames.account(a.id) : '';
      const ccys = [];
      const markets = (a && a.markets && typeof a.markets === 'object') ? Object.keys(a.markets) : [];
      markets.forEach((mk) => {
        const c = MARKET_CCY[mk];
        if (c && ccys.indexOf(c) === -1) ccys.push(c);
      });
      if (!ccys.length && a) {
        const legacy = a.ccy || a.settlement_ccy;
        if (legacy) ccys.push(legacy);
      }
      return ccys.length ? name + '（' + ccys.join('／') + '）' : name;
    }
  };
})();
