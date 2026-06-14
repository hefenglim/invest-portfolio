"""Layer assembly (spec 04.0): system + strategies + active calibration → one prompt.

Composes an insight_type's prompt in the HARD order (spec 4.0):

    system prompt (if use_system_prompt)
    + strategy 1 .. n (ordered by position, ENABLED + non-archived only)
    + active calibration version (only when self_correct AND a non-archived active version)

Each layer body (a ``{{var}}`` template) is rendered via the 06a ``render_prompt`` over the
FED :class:`~llm_insight.variables.VarContext`, so this layer recomputes no number. The
calibration may only append; it never rewrites an upper layer (spec 4.0) — assembly simply
concatenates in order, it does not merge.

Pure over fed inputs: stdlib + ``shared`` + ``llm_insight`` internals only. It reads the
composer / system-prompt tables (both pure ``llm_insight`` persistence) but imports neither
``pricing`` nor ``data_ingestion`` — those values arrive pre-computed in the VarContext
(architecture.md; the api service layer is the only seam that reads pricing/portfolio).
"""

import sqlite3
from typing import Literal

from pydantic import BaseModel

from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.llm_insight.system_prompt import get_system_prompt

LayerKind = Literal["system", "template", "calibration"]

# Joins the rendered layers into the single prompt sent to the model.
_LAYER_SEP = "\n\n"


class Layer(BaseModel):
    """One assembled prompt layer: its kind, a display name, and the rendered text."""

    kind: LayerKind
    name: str
    rendered: str


class Assembly(BaseModel):
    """The result of :func:`assemble_layers`: the layer list + joined prompt + tokens used.

    ``layers`` is for the 07 preview (per-layer inspection); ``prompt`` is what the LLM
    receives; ``tokens_used`` aggregates the registry tokens rendered across all layers.
    """

    layers: list[Layer]
    prompt: str
    tokens_used: list[str]


def assemble_layers(
    conn: sqlite3.Connection, insight_type_id: int, ctx: V.VarContext
) -> Assembly:
    """Assemble an insight_type's prompt layers in the spec-4.0 hard order.

    Renders each layer body against *ctx* (the fed VarContext). Disabled/archived
    strategies are skipped; the calibration layer is appended only when the insight_type
    has ``self_correct`` on AND a non-archived active calibration version exists. Returns
    the ordered layers, their concatenation, and the aggregated tokens used.
    """
    it = cs.get_insight_type(conn, insight_type_id)
    layers: list[Layer] = []
    tokens_used: list[str] = []

    def _render(body: str) -> str:
        rendered, used = V.render_prompt(body, ctx)
        for tok in used:
            if tok not in tokens_used:
                tokens_used.append(tok)
        return rendered

    if it is None:
        return Assembly(layers=[], prompt="", tokens_used=[])

    # 1) system prompt (optional).
    if it.use_system_prompt:
        body = get_system_prompt(conn)["body"]
        layers.append(Layer(kind="system", name="system", rendered=_render(body)))

    # 2) strategies in position order, enabled + non-archived only.
    for ref in cs.get_strategies(conn, insight_type_id):
        sp = cs.get_strategy(conn, ref.id)
        if sp is None or not sp.enabled or sp.archived:
            continue
        layers.append(Layer(kind="template", name=sp.name, rendered=_render(sp.body)))

    # 3) active calibration (only when self_correct AND a live active version exists).
    if it.self_correct and it.active_calibration_version is not None:
        active = next(
            (
                c
                for c in cs.list_calibrations(conn, insight_type_id)
                if c.version == it.active_calibration_version
            ),
            None,
        )
        if active is not None:
            layers.append(
                Layer(
                    kind="calibration",
                    name=f"calibration v{active.version}",
                    rendered=_render(active.body),
                )
            )

    prompt = _LAYER_SEP.join(lyr.rendered for lyr in layers)
    return Assembly(layers=layers, prompt=prompt, tokens_used=tokens_used)
