"""The ledger audit trail, read back (post-closure item 10, owner 2026-09-30).

Every edit / delete of a ledger row writes the row's BEFORE image to ``ledger_audit``
(audit M9, ``data_ingestion/store.py::_write_audit``). For months nothing read it: the demo
held 353 rows, ``store.list_ledger_audit`` had no caller, and the trades page's promise
「原值留存稽核軌跡」 pointed at a table no page could open. The owner ruled a READ-ONLY list in
資料中心 plus a CSV in the export centre.

This module is the ONE presentation of an audit row, shared by ``GET /api/ledger-audit`` and
the CSV, so the list and the file cannot label a column differently:

* **zh words for identifiers.** Ledger names come from ``shared/ledger_registry.py`` (the same
  labels the db-stats table beside the list prints), actions are 編輯／刪除, and every column
  of every audited table has a zh label — ``tests/contract/test_pc10_ledger_audit_reader.py``
  derives both sets from the code, so a new column cannot reach the page as a raw name.
* **Accounts by token (DEF-044 / DEF-045).** ``account_id`` values — and the 期初庫存 row key,
  ``<account>/<symbol>``, the one key that embeds an account — are rendered as
  ``{account:<id>}``, which ``web/api.js`` resolves on every response and every downloaded
  text file. The raw id appears only in the CSV's ``account_id`` column (a key column, like
  every reconciliation CSV) and in the JSON's machine fields (``row_id``, ``account_id``).
* **Values as stored.** The before-image is the record, so a value is printed as it was
  written: a Decimal column in its canonical fixed-point form (``stored_decimal_str`` — a
  legacy ``1E+2`` would otherwise reach a spreadsheet as text), a flag as 是／否, a side or a
  kind in its existing zh vocabulary, NULL as ``None`` (the page prints its null glyph).
* **The app clock.** ``at`` is stored in UTC; both surfaces show Asia/Taipei, because
  ``web/format.js::datetime`` slices a string it trusts to be local already.
"""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from portfolio_dash.data_ingestion.store import count_ledger_audit, list_ledger_audit
from portfolio_dash.export.artifact import ExportArtifact, csv_artifact
from portfolio_dash.shared.account_ref import account_ref
from portfolio_dash.shared.cash_kinds import CASH_KIND_ZH
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.corporate_actions import KIND_ZH
from portfolio_dash.shared.ledger_registry import LEDGER_TABLES
from portfolio_dash.shared.wire import stored_decimal_str

#: Audited table -> zh name. The six ledgers carry their registry label (what 資料庫統計 on
#: the same page prints); ``instruments`` is audited by the purge door (``delete_instrument``).
TABLE_LABELS: dict[str, str] = {
    **{t.table: t.label for t in LEDGER_TABLES},
    "instruments": "標的清單",
}

#: ``ledger_audit.action`` (a CHECK-constrained pair) -> the verb the owner reads.
ACTION_LABELS: dict[str, str] = {"update": "編輯", "delete": "刪除"}

#: Column -> zh label, over every column of every audited table (one namespace: a column
#: name means the same thing in each ledger that has it).
FIELD_LABELS: dict[str, str] = {
    "id": "編號",
    "account_id": "帳戶",
    "symbol": "代號",
    "side": "買賣",
    "quantity": "股數",
    "price": "成交價",
    "fees": "手續費",
    "tax": "交易稅",
    "trade_date": "交易日",
    "fee_rule_snapshot": "費率快照",
    "note": "備註",
    "daytrade": "當沖",
    "short_sale": "放空",
    "import_batch_id": "匯入批次",
    "source_row_hash": "來源列指紋",
    "date": "日期",
    "type": "股利類型",
    "gross": "總額",
    "withholding": "預扣稅",
    "net": "淨額",
    "reinvest_shares": "再投資股數",
    "reinvest_price": "再投資價格",
    "ex_date": "除息／除權日",
    "from_ccy": "換出幣別",
    "from_amount": "換出金額",
    "to_ccy": "換入幣別",
    "to_amount": "換入金額",
    "shares": "股數",
    "original_cost_total": "原始總成本",
    "build_date": "建檔日",
    "kind": "類別",
    "ccy": "幣別",
    "amount": "金額",
    "acq_home_amount": "取得成本（本國幣）",
    "corporate_action_id": "所屬公司行動",
    "rebate_period": "折讓款月份",
    "from_symbol": "原代號",
    "to_symbol": "新代號",
    "ratio_to": "比例（新股數）",
    "ratio_from": "比例（原股數）",
    "cost_carry": "成本承接比例",
    "band_move_json": "目標帶移轉紀錄",
    "weight_move_json": "目標權重移轉紀錄",
    "child_seed_json": "子股開盤價紀錄",
    "market": "市場",
    "quote_ccy": "報價幣別",
    "sector": "產業類別",
    "name": "名稱",
    "board": "掛牌板別",
    "target_low": "目標價下緣",
    "target_high": "目標價上緣",
    "board_status": "掛牌狀態",
    "is_etf": "ETF",
    "etf_flag_unknown": "ETF 待確認",
    "archived": "已封存",
    "industry": "產業",
    "target_set_at": "目標帶設定日",
}

#: Columns stored as Decimal TEXT — printed in the canonical fixed-point form.
_DECIMAL_FIELDS = frozenset({
    "quantity", "price", "fees", "tax", "gross", "withholding", "net", "reinvest_shares",
    "reinvest_price", "from_amount", "to_amount", "shares", "original_cost_total", "amount",
    "acq_home_amount", "ratio_to", "ratio_from", "cost_carry", "target_low", "target_high",
})
#: 0/1 INTEGER flags.
_FLAG_FIELDS = frozenset({"daytrade", "short_sale", "is_etf", "etf_flag_unknown", "archived"})
_SIDE_ZH = {"BUY": "買", "SELL": "賣"}
#: The fields a one-line summary is built from, in reading order (whichever the row has).
_SUMMARY_FIELDS = ("account_id", "symbol", "from_symbol", "name", "trade_date", "date",
                   "build_date", "side", "kind")
_CSV_HEADER = ["audit_id", "at", "table", "table_label", "row", "action", "action_label",
               "source", "account_id", "before"]


@dataclass(frozen=True)
class AuditField:
    """One column of a before-image: its name, zh label and display value (None = NULL)."""

    field: str
    label: str
    value: str | None


@dataclass(frozen=True)
class AuditEntry:
    """One ``ledger_audit`` row, presented."""

    id: int
    at: str
    table: str
    table_label: str
    row_id: str
    row_label: str
    action: str
    action_label: str
    source: str | None
    account_id: str | None
    summary: str
    fields: list[AuditField]

    def to_wire(self) -> dict[str, object]:
        return {
            "id": self.id, "at": self.at, "table": self.table,
            "table_label": self.table_label, "row_id": self.row_id,
            "row_label": self.row_label, "action": self.action,
            "action_label": self.action_label, "source": self.source,
            "account_id": self.account_id, "summary": self.summary,
            "fields": [{"field": f.field, "label": f.label, "value": f.value}
                       for f in self.fields],
        }


def _local(at: str) -> str:
    """A stored UTC ISO stamp in the app timezone; unparseable text passes through."""
    try:
        return datetime.fromisoformat(at).astimezone(
            ZoneInfo(get_settings().tz_display)).isoformat()
    except ValueError:
        return at


def _text(value: object) -> str:
    """A JSON scalar as text, without ``str()`` on anything that could be a Decimal."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return format(value, "d")
    return json.dumps(value, ensure_ascii=False)


def _display(table: str, field: str, value: object) -> str | None:
    if value is None:
        return None
    if field == "account_id" and isinstance(value, str):
        return account_ref(value)
    if field in _FLAG_FIELDS and isinstance(value, int):
        return "是" if value else "否"
    text = _text(value)
    if field in _DECIMAL_FIELDS:
        return stored_decimal_str(text) or text
    if field == "side":
        return _SIDE_ZH.get(text.upper(), text)
    if field == "kind":
        vocab = CASH_KIND_ZH if table == "cash_movements" else KIND_ZH
        return vocab.get(text.upper(), text)
    return text


def _before(raw: object) -> dict[str, object] | None:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def audit_entry(row: Mapping[str, object]) -> AuditEntry:
    """Present one ``ledger_audit`` row (as ``list_ledger_audit`` returns it).

    A before-image that is not a JSON object degrades to ONE field carrying the stored text —
    the reader never 500s on a row it cannot parse, and never hides it either.
    """
    table = _text(row["table_name"])
    row_id = _text(row["row_id"])
    action = _text(row["action"])
    before = _before(row["before_json"])
    if before is None:
        fields = [AuditField("before_json", "原內容", _text(row["before_json"]))]
        before = {}
    else:
        fields = [AuditField(k, FIELD_LABELS.get(k, k), _display(table, k, v))
                  for k, v in before.items()]
    shown = {f.field: f.value for f in fields}
    account = before.get("account_id")
    account_id = account if isinstance(account, str) else None
    row_label = row_id
    if table == "opening_inventory" and account_id is not None and "symbol" in before:
        # The one row key that embeds an account (``<account>/<symbol>``): named by token.
        row_label = f"{account_ref(account_id)}／{_text(before['symbol'])}"
    summary = "・".join(v for k in _SUMMARY_FIELDS if (v := shown.get(k)) is not None)
    source = row["source"]
    return AuditEntry(
        id=int(_text(row["id"])),
        at=_local(_text(row["at"])),
        table=table,
        table_label=TABLE_LABELS.get(table, table),
        row_id=row_id,
        row_label=row_label,
        action=action,
        action_label=ACTION_LABELS.get(action, action),
        source=source if isinstance(source, str) else None,
        account_id=account_id,
        summary=summary,
        fields=fields,
    )


def list_entries(
    conn: sqlite3.Connection, *, limit: int, offset: int
) -> tuple[list[AuditEntry], int]:
    """One page of the trail, newest first, and the trail's total size."""
    rows = list_ledger_audit(conn, limit=limit, offset=offset)
    return [audit_entry(r) for r in rows], count_ledger_audit(conn)


def _before_text(entry: AuditEntry) -> str:
    """The before-image as one readable cell: 「股數：100；成交價：600；…」."""
    return "；".join(f"{f.label}：{'—' if f.value is None else f.value}" for f in entry.fields)


def build_ledger_audit_csv(
    conn: sqlite3.Connection, *, frm: str | None, to: str | None
) -> ExportArtifact:
    """The whole trail, oldest first (like the other log exports), range-filtered on the
    app-clock DAY of ``at``."""
    body: list[list[str]] = []
    for r in reversed(list_ledger_audit(conn)):
        e = audit_entry(r)
        day = e.at[:10]
        if (frm and day < frm) or (to and day > to):
            continue
        body.append([format(e.id, "d"), e.at, e.table, e.table_label, e.row_label, e.action,
                     e.action_label, e.source or "", e.account_id or "", _before_text(e)])
    tag = f"{frm or 'all'}_{to or 'all'}"
    return csv_artifact(f"ledger_audit_{tag}.csv", header=_CSV_HEADER, rows=body)
