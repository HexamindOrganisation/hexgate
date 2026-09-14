"""The framework-agnostic half of every adapter's ``messages.py``.

Each adapter converts its own framework's message types into the OTel GenAI
``role``/``parts`` shape, and the *item shapes* are what differ — the
normalisation around them is not. Both helpers below were written once for the
OpenAI Agents adapter and were about to be copied verbatim into the LangChain
one, which is the point the design doc named for hoisting them (§III SDK,
"hoist the framework-agnostic half once a second adapter exists"): one copy is
not duplication, two is.

Internal, like ``adapters/_common.py`` — each adapter's own ``messages.py`` is
the supported import surface.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def as_dict(item: Any) -> dict[str, Any] | None:
    """One framework message (or content part) as a plain dict, or ``None`` if
    it is neither a mapping nor a pydantic model.

    Three of the four frameworks are covered: OpenAI Agents' input items are
    TypedDicts and its output items pydantic models, and LangChain's
    ``BaseMessage`` and google-genai's ``Content``/``Part`` are pydantic.
    pydantic-ai is the exception despite the name — its ``ModelMessage`` and
    every ``*Part`` are stdlib ``@dataclass``es with no ``model_dump``, so that
    adapter will need a dataclass branch here rather than this as it stands.
    Each converter reads ``type``/``role`` off the result to decide a message's
    shape, which needs a dict in hand; ``None`` is the caller's cue to fall
    back rather than guess at an object it cannot open.
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
