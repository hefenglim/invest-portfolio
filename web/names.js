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

   NOT covered here (by design): surfaces that render an account label straight from
   the API payload (`account_name` from /api/dashboard rows, `name` from
   /api/input/context selects — e.g. rebalance legs, cash statements, the input/cash
   selects). Those are the API-fed forms; unifying them is the job of the PLANNED
   SUCCESSOR — a server-side `account.display_name` field carried on /api/* — which is
   deferred to a future golden-payload re-baseline. Until then this file is the single
   naming authority for the frontend's map-based surfaces only.

   Load this BEFORE any dependent script (app.js / detail.js / ledger.js). Dependents
   degrade gracefully (id fallback, no crash) if it is absent. */
(function () {
  'use strict';

  /* id -> { name: full canonical zh, short: compact zh for chips }. Closed set: the
     four first-class, config-seeded accounts (CLAUDE.md — account is a first-class
     entity). An unknown id falls through to the id itself (see the resolver below). */
  const ACCOUNTS = {
    tw_broker:    { name: '台灣券商',    short: '台灣券商' },
    schwab:       { name: '嘉信 Schwab', short: '嘉信' },
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
