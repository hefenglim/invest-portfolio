"""Test doubles for the AI door's completion seam (``data_ingestion.agents.Completer``).

The door takes the ``complete_structured_meta`` shape — the parsed value PLUS which model
produced it and which failed first (DEF-083 / DEF-084) — so a fake that only knows the
parsed value is wrapped here rather than each test building a ``StructuredCompletion``.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from portfolio_dash.shared.llm import StructuredCompletion

FAKE_MODEL = "fake-model"


def completing[T: BaseModel](
    fn: Callable[..., T], *, model: str = FAKE_MODEL
) -> Callable[..., StructuredCompletion[T]]:
    """Wrap a fake returning the parsed value into the completion shape (no failover)."""

    def call(*args: Any, **kwargs: Any) -> StructuredCompletion[T]:
        return StructuredCompletion(
            value=fn(*args, **kwargs), model=model, cost=Decimal("0"), model_name=model,
        )

    return call
