"""The framework-agnostic half of every adapter's ``messages.py``.

Each adapter converts its own framework's message types into the OTel GenAI
``role``/``parts`` shape; the item shapes differ, the normalisation around them
does not. Hoisted here when the LangChain adapter was about to copy both
verbatim — the point the design doc named for it (§III SDK).

Internal, like ``adapters/_common.py``: each adapter's own ``messages.py`` is
the supported import surface.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def as_dict(item: Any) -> dict[str, Any] | None:
    """One framework message (or content part) as a plain dict, or ``None`` if
    it is neither a mapping nor a pydantic model.

    Covers three of the four frameworks: OpenAI Agents' items are TypedDicts
    and pydantic models, LangChain's ``BaseMessage`` and google-genai's
    ``Content``/``Part`` are pydantic. pydantic-ai is the exception despite the
    name — its ``ModelMessage`` and every ``*Part`` are stdlib dataclasses with
    no ``model_dump``, so that adapter will need a branch of its own here.
    ``None`` is the caller's cue to fall back rather than guess at an object it
    cannot open.
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
