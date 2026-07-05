"""Master-role LLM orchestration (spec 04.3 / 4.5 / 4.8): scoring, calibration, validator.

The "master" model (high-reasoning, config-bound) does three qualitative jobs:

1. :func:`score_narrative` — rate a matured insight's narrative accuracy (0–100) + a miss
   verdict + a short cause note, given the card text + the create-time snapshot + the actual
   outcome. (The quant verdict is computed separately by the pure ``scoring.score_quant``.)
2. :func:`generate_calibration` — write a COMPLETE new calibration version from the active
   body + the miss samples + the confidence bins. Its system prompt carries the §4.8 safety
   lock (append-only / reconstruct-and-trim old logic / word cap / no vague predictionless
   filler).
3. :func:`validate_calibration` — gate a candidate body: a deterministic keyword denylist
   (越權 / 幣別混算 phrases) FIRST, then one master LLM review pass. Invalid → rejected
   (the job does not write the version).

LOCKED layering (architecture.md): PURE ``llm_insight`` — stdlib + pydantic + ``shared``
only. The LLM seam is ``shared.llm`` (the same ``litellm.completion`` the default role uses,
just a different model row). No pricing / data_ingestion / api import; no money math.
Master unset → ``shared.llm`` raises :exc:`AINotActivated` and the self-correct pipeline
pauses (cards still generate; spec 4.3).
"""

import sqlite3
from typing import Any

from pydantic import BaseModel

from portfolio_dash.shared import llm
from portfolio_dash.shared.llm_config import LLMRole

# --- agent labels (recorded in llm_usage, role=master) ------------------------
_AGENT_SCORE = "master_score"
_AGENT_CALIBRATE = "master_calibrate"
_AGENT_VALIDATE = "master_validate"

# --- §4.8 deterministic denylist (checked BEFORE the LLM review) --------------
# Phrases that signal a calibration overstepping its remit (giving position/trade advice)
# or instructing currency mixing — both are hard "越權"/"幣別混算" violations (spec 4.5/4.8).
_DENYLIST: tuple[str, ...] = (
    "幣別混算",
    "混算",
    "調整持倉",
    "調整部位",
    "加碼",
    "減碼",
    "買進",
    "賣出",
    "停損",
    "停利",
)

# The §4.8 self-correct safety lock, carried in the calibration-generation system prompt.
_CALIBRATION_SYSTEM = (
    "<role>你是投資洞察系統的校正大師。你只負責改寫『敘事品質校正規則』，"
    "絕不下達任何買賣、加減碼、調整持倉部位的越權建議，也絕不要求幣別混算。</role>\n"
    "<safety_lock>\n"
    "1. 校正規則只能附加/精煉：新增規則時必須重構並精簡既有邏輯，避免條款膨脹。\n"
    "2. 全文總字數不得超過 600 字（精簡為先）。\n"
    "3. 不得為了避免失誤而產出含糊、無預測價值的廢話；每條規則都要可檢驗。\n"
    "4. 保留仍有效的條款，只修訂已失效的；個股層級的失誤寫成「（個股）…」條款。\n"
    "5. 時效優先：校正規則應要求洞察標注資料基準日，並以近期資料為主要判讀依據。\n"
    "</safety_lock>\n"
    "<output>僅回傳 JSON：{\"body\": \"完整新版校正規則\", \"cause\": \"本次修訂原因\"}，"
    "不要 Markdown 圍欄、不要額外散文。</output>"
)

# v2 rubric (2026-07-05 audit §2.4): the narrative score is Loop-3's LEARNING SIGNAL —
# without dimensions/anchors/a miss definition, the calibration loop learns from noise.
_SCORE_SYSTEM = (
    "<role>你是投資洞察回測評分大師。輸入為一張到期洞察卡的原文、產卡當時的輸入快照、"
    "以及到期時的實際結果。請依下列準則評估該卡的『敘事準確度』。</role>\n"
    "<rubric>\n"
    "四維度加權（總分 0-100）：\n"
    "1. 方向正確性 40%：卡片的方向性判讀（偏多／偏空／觀望）與實際走勢的相符程度。\n"
    "2. 數字引用正確性 30%：卡內引用的數字是否忠於產卡當時的輸入快照；"
    "引用快照中不存在的數字＝捏造，重罰。\n"
    "3. 條件情境效度 20%：卡片給的條件式情境（如「跌破 60 日均線宜重新評估」）"
    "是否被實際走勢觸發且有效。\n"
    "4. 時間性 10%：卡片是否標注資料基準日、結論是否與資料時效相符"
    "（把過期資料當即時資料使用＝扣分）。\n"
    "分數錨：90-100＝方向對、數字全對、情境被驗證；70-89＝方向對但有小瑕疵；"
    "50-69＝方向模糊或部分引用有誤；30-49＝方向錯但有誠實的避險語；"
    "0-29＝方向錯且引用捏造或誤導。\n"
    "miss=true 的定義：方向判讀與實際走勢相反，或卡內出現快照中不存在的捏造數字。"
    "資料不足以判定時維持 miss=false 並在 note 說明原因。\n"
    "</rubric>\n"
    "<rules>數字一律以輸入為準，不得自行捏造價格或報酬；只評敘事品質，不重算損益。"
    "note 必須引用具體證據（卡內原句對照實際數字），不接受空泛評語。</rules>\n"
    "<output>僅回傳 JSON："
    "{\"narrative_score\": 0-100 整數, \"miss\": true|false, \"note\": \"具體證據簡評\"}，"
    "不要 Markdown 圍欄、不要額外散文。</output>"
)

_VALIDATE_SYSTEM = (
    "<role>你是校正規則安全審查員。審查一段候選校正規則是否越權"
    "（下達買賣/加減碼/調整持倉等投資指令）或要求幣別混算。</role>\n"
    "<output>僅回傳 JSON：{\"ok\": true|false, \"reasons\": [\"...\"]}；"
    "ok=true 表示安全可採用。不要 Markdown 圍欄。</output>"
)


class _NarrativeScore(BaseModel):
    narrative_score: int
    miss: bool
    note: str = ""


class _Calibration(BaseModel):
    body: str
    cause: str = ""


class _Review(BaseModel):
    ok: bool
    reasons: list[str] = []


def score_narrative(
    *,
    card_text: str,
    snapshot_then: str,
    actual_now: str,
    eval_prompt: str | None,
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Master-role narrative score for a matured insight (spec 4.4 step 3).

    Returns ``{"narrative_score": int, "miss": bool, "note": str}``. Uses the optional
    per-task ``eval_prompt`` (custom检验 template) when set, else the standard master-scoring
    template. Raises :exc:`AINotActivated` when the master role is unset (caller skips
    narrative scoring and falls back to quant-only — degrade, never crash).
    """
    extra = f"\n<custom_eval_prompt>\n{eval_prompt}\n</custom_eval_prompt>" if eval_prompt else ""
    prompt = (
        f"{_SCORE_SYSTEM}{extra}\n"
        f"<card>\n{card_text}\n</card>\n"
        f"<input_snapshot_at_creation>\n{snapshot_then}\n</input_snapshot_at_creation>\n"
        f"<actual_outcome>\n{actual_now}\n</actual_outcome>"
    )
    out = llm.complete_structured(
        prompt, _NarrativeScore, agent=_AGENT_SCORE, conn=conn, role=LLMRole.MASTER
    )
    return {"narrative_score": out.narrative_score, "miss": out.miss, "note": out.note}


def generate_calibration(
    *,
    active_body: str,
    miss_samples: list[dict[str, Any]],
    bins: list[dict[str, Any]],
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Master-role generation of a COMPLETE new calibration version (spec 4.5).

    Returns ``{"body": str, "cause": str}``. The system prompt carries the §4.8 safety lock.
    Raises :exc:`AINotActivated` when the master role is unset. The caller validates the body
    (:func:`validate_calibration`) before persisting it.
    """
    # v2 (2026-07-05 audit §2.5): render the FULL failure context — the failed claim
    # (title/summary/prediction), the score/confidence, and the actual outcome — so the
    # master rewrites rules from first-hand evidence, not just the scorer's note.
    def _sample_line(s: dict[str, Any]) -> str:
        parts = [f"- insight {s.get('insight_id')}"]
        if s.get("card_symbol"):
            parts.append(f"[{s['card_symbol']}]")
        if s.get("card_title"):
            parts.append(f"「{s['card_title']}」")
        if s.get("card_summary"):
            parts.append(f"主張：{s['card_summary']}")
        if s.get("card_prediction"):
            parts.append(f"預測：{s['card_prediction']}")
        if s.get("confidence") is not None:
            parts.append(f"信心 {s['confidence']}")
        if s.get("actual_value") is not None:
            parts.append(f"實際 {s['actual_value']}")
        if s.get("narrative_score") is not None:
            parts.append(f"敘事分 {s['narrative_score']}")
        if s.get("notes"):
            parts.append(f"評語：{s['notes']}")
        return " ｜ ".join(parts)

    samples_text = "\n".join(_sample_line(s) for s in miss_samples) or "（無失誤樣本）"
    bins_text = "\n".join(
        f"- {b.get('bucket')}: 校準誤差 {b.get('calibration_error_pp')}pp" for b in bins
    ) or "（無分桶資料）"
    prompt = (
        f"<active_calibration>\n{active_body}\n</active_calibration>\n"
        f"<miss_samples>\n{samples_text}\n</miss_samples>\n"
        f"<calibration_bins>\n{bins_text}\n</calibration_bins>\n"
        "請依安全鎖產出完整新版校正規則。"
    )
    out = _master_structured(
        prompt, _Calibration, agent=_AGENT_CALIBRATE, conn=conn, system=_CALIBRATION_SYSTEM
    )
    return {"body": out.body, "cause": out.cause}


def validate_calibration(body: str, *, conn: sqlite3.Connection) -> tuple[bool, list[str]]:
    """Gate a candidate calibration body (spec 4.8): keyword denylist + one LLM review.

    Returns ``(ok, reasons)``. The deterministic denylist runs FIRST and short-circuits a
    hard violation (no LLM cost). A clean body then gets a single master review pass. Raises
    :exc:`AINotActivated` only when the body passes the denylist but the master role is unset.
    """
    hits = [kw for kw in _DENYLIST if kw in body]
    if hits:
        return False, [f"越權/幣別混算關鍵字：{kw}" for kw in dict.fromkeys(hits)]
    prompt = (
        f"<candidate_calibration>\n{body}\n</candidate_calibration>\n請審查是否安全可採用。"
    )
    review = _master_structured(
        prompt, _Review, agent=_AGENT_VALIDATE, conn=conn, system=_VALIDATE_SYSTEM
    )
    return review.ok, list(review.reasons)


def _master_structured[T: BaseModel](
    prompt: str, schema: type[T], *, agent: str, conn: sqlite3.Connection, system: str
) -> T:
    """A master-role structured call with a system message prepended into the prompt.

    ``complete_structured`` builds a single user message, so the system instruction is folded
    into the prompt text (the seam has no separate system slot for structured calls). Keeps
    one code path for the master JSON calls.
    """
    return llm.complete_structured(
        f"{system}\n{prompt}", schema, agent=agent, conn=conn, role=LLMRole.MASTER
    )
