"""Wire-format serialization (re-export).

The implementation moved to ``portfolio_dash.shared.wire`` so lower layers
(``llm_insight``, ``export``) can use it without importing the web layer
(architecture.md: lower layers never import ``api``). This module re-exports
:func:`to_wire` unchanged so every existing ``api.serialize.to_wire`` caller keeps working.
"""

from portfolio_dash.shared.wire import to_wire

__all__ = ["to_wire"]
