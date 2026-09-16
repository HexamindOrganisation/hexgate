"""The framework-agnostic half of every adapter's ``messages.py``: the item
shapes each one converts differ, the normalisation around them does not.

Hoisted here when the LangChain adapter was about to copy both verbatim, the
point the design doc named for it (§III SDK). Internal, like
``adapters/_common.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def as_dict(item: Any) -> dict[str, Any] | None:
    """One framework message (or content part) as a plain dict, or ``None`` as
    the caller's cue to fall back rather than guess at what it cannot open.

    Covers three of the four frameworks; pydantic-ai is the exception despite
    the name, since its ``ModelMessage`` and every ``*Part`` are stdlib
    dataclasses with no ``model_dump``.
    """
    if isinstance(item, Mapping):
        return dict(item)
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return None


def text_part(content: Any) -> dict[str, Any]:
    """A GenAI ``text`` part. The one part shape every framework produces."""
    return {"type": "text", "content": content}
