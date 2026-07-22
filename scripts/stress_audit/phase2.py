"""Phase 2 — investor-realistic stress on the LIVE DEMO site, UI-first.

Additive on top of the demo's existing seeded data (NEVER reset/delete pre-existing).
Reconciliation is ABSOLUTE (the oracle reads the demo's FULL current ledger via the
public read endpoints and recomputes; compared to the demo's computed outputs) PLUS
explicit DELTA assertions (post-state = baseline + oracle-predicted deltas) for the
cash pools touched and the instruments newly registered (absolute-from-zero).

No FX rates are exposed to the harness, so reporting-currency BLENDED KPIs (including
XIRR) are out of scope here; every native-currency figure (cost basis, cash pools,
realized P&L, FX pool avg-rate/realized, per-holding valuation vs the demo's OWN price)
is reconciled exactly.
"""

from __future__ import annotations

import time
from decimal import Decimal

import common as C
import oracle as O
from common import dec

D = Decimal

# NEW instruments (absent from the demo baseline probe) -> absolute-from-zero coverage.
NEW_INSTRUMENTS = [
    ("MSFT", "US", "USD", "Microsoft", "Technology", False),
    ("TSLA", "US", "USD", "Tesla", "Auto", False),
    ("3008", "TW", "TWD", "LARGAN", "Optics", False),
    ("5225", "MY", "MYR", "IHH Healthcare", "Healthcare", False),
]

# symbol -> market, so the fee check can route the merged dual-market moomoo_my's per-market rule
# (Batch B): a moomoo_my US trade books moomoo_us fees, an MY trade books moomoo_my fees.
_MARKET_OF = {sym: mkt for sym, mkt, *_ in NEW_INSTRUMENTS}


class Ops2:
    def __init__(self, ev: C.Evidence, api: C.Api, ui, phase="phase2"):
        self.ev = ev
        self.api = api
        self.ui = ui
        self.phase = phase

    # ---- counts / lookups via API ----
    def _tx_total(self):
        return self.api.get("/api/ledgers/transactions", limit=1).json().get("total_count", 0)

    def _div_total(self):
        return self.api.get("/api/ledgers/dividends", limit=1).json().get("total_count", 0)

    def _fx_total(self):
        return self.api.get("/api/ledgers/fx", limit=1).json().get("total_count", 0)

    def _cash_total(self):
        return (self.api.get("/api/cash", limit=1).json().get("movements", {})
                .get("total_count", 0))

    def _wait_increase(self, fn, before, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if fn() > before:
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _latest_tx_id(self, account_id, symbol, side):
        rows = self.api.get("/api/ledgers/transactions", limit=500).json().get("rows", [])
        cand = [r for r in rows if r["account_id"] == account_id and r["symbol"] == symbol
                and str(r["side"]).upper() == side.upper()]
        return max((r["id"] for r in cand), default=None)

    def _fee_check(self, txn_id, account_id, side, qty, price, symbol, is_etf):
        got = C.read_fee_tax_from_api(self.api, txn_id)
        if got is None:
            return
        # Batch B: route the merged moomoo_my's fee rule by the instrument's market. The demo
        # exposes no USD/MYR rate, so a moomoo_my US trade's MY stamp is 0 on both sides
        # (stamp_fx=None here == the app's None) — native fees still reconcile exactly.
        market = _MARKET_OF.get(symbol)
        fee, tax, _ = O.fee_tax(account_id, side.upper(), dec(qty), dec(price), is_etf,
                                market=market)
        self.ev.check("fee_engine.fee", f"{account_id}/{symbol} {side} {qty}@{price} id={txn_id}",
                      fee, got[0], self.phase)
        self.ev.check("fee_engine.tax", f"{account_id}/{symbol} {side} {qty}@{price} id={txn_id}",
                      tax, got[1], self.phase)

    # ---- setup ----
    def register(self, symbol, market, ccy, name, sector, is_etf):
        body = {"symbol": symbol, "market": market, "name": name, "sector": sector,
                "is_etf": is_etf}
        r = self.api.post("/api/instruments", body)
        resp = {"status": r.status_code, "json": _j(r)}
        self.ev.op(self.phase, "API", "register.instrument", body, resp,
                   note="watchlist (setup)")
        return resp

    # ---- money flows ----
    def trade(self, account_id, symbol, side, d, shares, price, *, via_ui=False,
              ack=False, expect=201, fee_check=True, is_etf=False):
        body = {"account_id": account_id, "symbol": symbol, "side": side, "date": d,
                "shares": str(shares), "price": str(price), "ack_oversell": ack}
        if via_ui and self.ui is not None:
            before = self._tx_total()
            self.ui.manual_trade(account_id, symbol, side, d, shares, price)
            ok = self._wait_increase(self._tx_total, before)
            txn_id = self._latest_tx_id(account_id, symbol, side) if ok else None
            resp = {"status": 201 if ok else 0, "json": {"txn_id": txn_id}}
            surface = "UI"
        else:
            r = self.api.post("/api/input/manual/commit", body)
            resp = {"status": r.status_code, "json": _j(r)}
            surface = "API"
        self.ev.op(self.phase, surface, f"trade.{side}", body, resp, note=f"{account_id} {symbol}")
        if resp["status"] == expect and expect == 201 and fee_check:
            txn_id = resp["json"].get("txn_id") if isinstance(resp.get("json"), dict) else None
            if txn_id:
                self._fee_check(txn_id, account_id, side, shares, price, symbol, is_etf)
        return resp

    def cash_move(self, account_id, kind, ccy, d, amount, *, via_ui=False, ack=False):
        body = {"account_id": account_id, "date": d, "kind": kind, "ccy": ccy,
                "amount": str(amount), "ack_negative": ack}
        if via_ui and self.ui is not None:
            before = self._cash_total()
            self.ui.cash_move(account_id, kind, ccy, d, amount)
            ok = self._wait_increase(self._cash_total, before)
            resp = {"status": 201 if ok else 0, "json": {}}
            surface = "UI"
        else:
            r = self.api.post("/api/cash/movements", body)
            resp = {"status": r.status_code, "json": _j(r)}
            surface = "API"
        self.ev.op(self.phase, surface, f"cash.{kind}", body, resp)
        return resp

    def fx(self, account_id, d, from_ccy, from_amt, to_ccy, to_amt, *, via_ui=False, ack=False):
        body = {"account_id": account_id, "date": d, "from_ccy": from_ccy,
                "from_amt": str(from_amt), "to_ccy": to_ccy, "to_amt": str(to_amt),
                "ack_negative": ack}
        if via_ui and self.ui is not None:
            before = self._fx_total()
            self.ui.fx(account_id, d, from_ccy, from_amt, to_ccy, to_amt)
            ok = self._wait_increase(self._fx_total, before)
            resp = {"status": 201 if ok else 0, "json": {}}
            surface = "UI"
        else:
            r = self.api.post("/api/cash/fx", body)
            resp = {"status": r.status_code, "json": _j(r)}
            surface = "API"
        self.ev.op(self.phase, surface, "fx.convert", body, resp)
        return resp

    def dividend_ui(self, account_id, model, symbol, d, gross, *, reinvest_price=None,
                    net=None):
        before = self._div_total()
        self.ui.manual_dividend(account_id, model, symbol, d, gross,
                                reinvest_price=reinvest_price, net=net)
        ok = self._wait_increase(self._div_total, before)
        resp = {"status": 201 if ok else 0, "json": {}}
        self.ev.op(self.phase, "UI", f"dividend.{model}",
                   {"account": account_id, "symbol": symbol, "date": d, "gross": str(gross)},
                   resp)
        return resp

    def edit_tx(self, txn_id, account_id, symbol, side, d, shares, price, fee, tax, *, ack=False):
        body = {"account_id": account_id, "symbol": symbol, "side": side, "date": d,
                "shares": str(shares), "price": str(price), "fee": str(fee), "tax": str(tax),
                "ack_oversell": ack}
        r = self.api.put(f"/api/ledgers/transactions/{txn_id}", body)
        resp = {"status": r.status_code, "json": _j(r)}
        self.ev.op(self.phase, "API", "edit.transaction", {"id": txn_id, **body}, resp)
        return resp

    def inbox_refresh_confirm(self, max_confirm=3):
        before = self._div_total()
        self.ui.inbox_refresh()
        pending = self.ui.inbox_pending_count()
        confirmed = self.ui.inbox_confirm(max_confirm=max_confirm) if pending else 0
        after = self._div_total()
        self.ev.op(self.phase, "UI", "dividend_inbox.refresh_confirm",
                   {"max_confirm": max_confirm},
                   {"pending_detected": pending, "confirmed_clicked": confirmed,
                    "div_ledger_before": before, "div_ledger_after": after})
        return {"pending": pending, "confirmed": confirmed, "written": after - before}


def _j(r):
    try:
        return r.json()
    except Exception:
        return {"text": r.text[:300]}


# ------------------------------------------------------------------ reconciliation
def snapshot(api: C.Api):
    facts = C.load_facts_from_api(api)
    res = O.replay(facts)
    dash = api.get("/api/dashboard").json()
    cash = api.get("/api/cash", limit=500).json()
    reported_hold = {(h["account_id"], h["symbol"]): h for h in dash["holdings"]}
    reported_cash = {(b["account_id"], b["ccy"]): dec(b["amount"]) for b in cash["balances"]}
    return {"facts": facts, "res": res, "dash": dash,
            "reported_hold": reported_hold, "reported_cash": reported_cash}


def reconcile_abs(ev: C.Evidence, api: C.Api, label: str, snap):
    """Absolute reconciliation: oracle(full demo ledger) vs demo computed."""
    phase = f"phase2:{label}"
    res = snap["res"]
    dash = snap["dash"]
    app_hold = snap["reported_hold"]
    app_bal = snap["reported_cash"]

    # holdings cost basis (ALL holdings)
    ev.check("holdings.keyset", label,
             sorted("|".join(k) for k in res.holdings),
             sorted("|".join(k) for k in app_hold), phase)
    for key, h in res.holdings.items():
        a = app_hold.get(key)
        if a is None:
            ev.check("holdings.present", "|".join(key), True, False, phase)
            continue
        sc = "|".join(key)
        ev.check("holding.shares", sc, h.shares, a["shares"], phase)
        ev.check("holding.original_total", sc, h.original_total, a["original_cost_total"], phase)
        ev.check("holding.adjusted_total", sc, h.adjusted_total, a["adjusted_cost_total"], phase)
        ev.check("holding.original_avg", sc, h.original_avg, a["original_avg"], phase)
        ev.check("holding.adjusted_avg", sc, h.adjusted_avg, a["adjusted_avg"], phase)
        # per-holding valuation self-consistency vs the demo's OWN market_price
        mp = a.get("market_price")
        if mp is not None and h.shares > O.ZERO:
            mp = dec(mp)
            ev.check("holding.market_value", sc, mp * h.shares, a.get("market_value"), phase)
            ev.check("holding.unrealized_pnl", sc, (mp - h.adjusted_avg) * h.shares,
                     a.get("unrealized_pnl"), phase)

    # cash pools + statement terminal
    for pool in sorted(set(res.cash) | set(app_bal)):
        ev.check("cash.balance", "|".join(pool), res.cash.get(pool, O.ZERO),
                 app_bal.get(pool, O.ZERO), phase)
    _cash_statement(ev, snap["facts"], app_bal, phase)

    # realized rows
    app_real = dash["realized"]["rows"]
    ev.check("realized.count", label, len(res.realized_rows), len(app_real), phase)
    for i, rr in enumerate(res.realized_rows):
        if i >= len(app_real):
            break
        a = app_real[i]
        sc = f"{rr.account_id}/{rr.symbol}@{rr.sell_date} #{i}"
        ev.check("realized.proceeds_net", sc, rr.proceeds_net, a["proceeds_net"], phase)
        ev.check("realized.adjusted_removed", sc, rr.adjusted_cost_removed,
                 a["adjusted_cost_removed"], phase)
        ev.check("realized.realized", sc, rr.realized, a["realized"], phase)

    # FX pool per account (native; only when the demo surfaces it)
    fx = dash.get("fx")
    if fx and fx.get("by_account"):
        for aid, (_rule, settle, funding) in O.ACCOUNTS.items():
            if settle == funding or aid not in fx["by_account"]:
                continue
            acc = fx["by_account"][aid]
            ev.check("fx.avg_rate", aid, res.fx_avg_rate.get(aid), acc.get("avg_rate"), phase)
            ev.check("fx.realized", aid, res.fx_realized.get(aid), acc.get("realized_fx"), phase)

    # exports (CSV): cost basis + realized, absolute
    _exports(ev, api, res, phase)
    return res


def _cash_statement(ev, facts, app_bal, phase):
    lines: dict[tuple, list] = {}

    def add(key, delta):
        lines.setdefault(key, []).append(delta)

    for m in facts.cash:
        add((m.account_id, m.ccy), m.amount if m.kind == "DEPOSIT" else -m.amount)
    for c in facts.fxs:
        add((c.account_id, c.from_ccy), -c.from_amt)
        add((c.account_id, c.to_ccy), c.to_amt)
    for t in facts.txs:
        inst = facts.instruments.get(t.symbol)
        if inst is None:
            continue
        d = -(t.qty * t.price + t.fee + t.tax) if t.side == "BUY" \
            else (t.qty * t.price - t.fee - t.tax)
        add((t.account_id, inst.quote_ccy), d)
    for dv in facts.divs:
        inst = facts.instruments.get(dv.symbol)
        if inst is None or dv.type not in O.CASH_DIVIDEND_TYPES:
            continue
        add((dv.account_id, inst.quote_ccy), dv.net)
    for key in sorted(set(lines) | set(app_bal)):
        run = O.ZERO
        for delta in lines.get(key, []):
            run += delta
        ev.check("cash.statement.terminal", "|".join(key), run, app_bal.get(key, O.ZERO), phase)


def _exports(ev, api, res, phase):
    raw = api.download("/api/export/holdings").decode("utf-8-sig")
    rows = _parse(raw)
    hk = {(r["account_id"], r["symbol"]): r for r in rows}
    for key, h in res.holdings.items():
        r = hk.get(key)
        if r is None:
            continue
        sc = "|".join(key)
        ev.check("export.holdings.original_cost_total", sc, h.original_total,
                 r["original_cost_total"], phase)
        ev.check("export.holdings.adjusted_cost_total", sc, h.adjusted_total,
                 r["adjusted_cost_total"], phase)
    raw = api.download("/api/export/realized").decode("utf-8-sig")
    rrows = _parse(raw)
    ev.check("export.realized.count", "count", len(res.realized_rows), len(rrows), phase)


def _parse(raw):
    import csv
    import io
    lines = [ln for ln in raw.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    return list(csv.DictReader(io.StringIO("\n".join(lines)))) if lines else []


def delta_asserts(ev: C.Evidence, base, post, touched_pools, new_symbols):
    """post-state == baseline + oracle-predicted deltas (owner requirement)."""
    phase = "phase2:delta"
    bres, pres = base["res"], post["res"]
    brep, prep = base["reported_cash"], post["reported_cash"]
    for pool in sorted(touched_pools):
        oracle_delta = pres.cash.get(pool, O.ZERO) - bres.cash.get(pool, O.ZERO)
        demo_delta = prep.get(pool, O.ZERO) - brep.get(pool, O.ZERO)
        ev.check("delta.cash_pool", "|".join(pool), oracle_delta, demo_delta, phase)
    bhold, phold = base["reported_hold"], post["reported_hold"]
    for key in sorted(new_symbols):
        base_present = any(k[1] == key for k in bhold)
        ev.check("delta.new_symbol.absent_at_baseline", key, False, base_present, phase)
        # absolute-from-zero: oracle holding for the new symbol == demo holding
        okeys = [k for k in pres.holdings if k[1] == key]
        for k in okeys:
            a = phold.get(k)
            if a is None:
                ev.check("delta.new_symbol.present_post", "|".join(k), True, False, phase)
                continue
            ev.check("delta.new_symbol.shares", "|".join(k),
                     pres.holdings[k].shares, a["shares"], phase)
            ev.check("delta.new_symbol.adjusted_total", "|".join(k),
                     pres.holdings[k].adjusted_total, a["adjusted_cost_total"], phase)
