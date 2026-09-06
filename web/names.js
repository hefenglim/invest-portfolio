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

  const asId = (id) => (id === null || id === undefined ? '' : String(id));

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
    }
  };
})();
