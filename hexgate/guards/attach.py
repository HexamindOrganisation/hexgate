"""Make a guard list travel with the agent object it guards.

Guards are authored as a flat ``guards=[...]`` list. For the platform to govern
them, the manifest has to *declare* them — and ``hexgate register`` /
``hexgate serve`` only ever hold the agent object (loaded by module path), never
the original list. So the list is stamped onto the agent under a private
attribute and read back at registration by :func:`hexgate.manifest.create_manifest`.

The agent object a caller registers is framework-specific, but all five accept a
private (leading-underscore) attribute and keep it out of their own serialization,
so the stamp never perturbs the manifest content hash:

- our ``HexgateAgent`` — a plain attribute on our own class;
- an OpenAI ``agents.Agent`` — a non-slotted dataclass, so an instance attribute
  is fine and, not being a declared field, never enters ``dataclasses.replace``;
- a Google ADK ``LlmAgent`` and a LangGraph ``CompiledStateGraph`` — pydantic
  models that accept a private attribute and exclude it from ``model_dump``;
- a Pydantic-AI ``Agent`` — a plain object.

Setting is wrapped in ``try/except``: if a future framework version refuses the
attribute, guards simply do not appear on the manifest — never a crash, and they
still run at execution time.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hexgate.guards.types import Guard

_log = logging.getLogger(__name__)

# Private, framework-agnostic attribute the guard list rides on. Underscore so
# every supported agent object accepts it and leaves it out of serialization.
GUARDS_ATTR = "_hexgate_guards"


def attach_guards[T](agent: T, guards: "Sequence[Guard] | None") -> T:
    """Stamp ``guards`` onto ``agent`` so it travels with the object.

    Returns ``agent`` so the call can be chained. A ``None`` or empty list is a
    no-op (nothing to declare). Fail-safe: if the object refuses the attribute,
    it warns and returns the agent unchanged — the guards still run, they just
    will not appear on the registered manifest.
    """
    if not guards:
        return agent
    stamp = tuple(guards)
    try:
        setattr(agent, GUARDS_ATTR, stamp)
    except Exception:  # noqa: BLE001 — a stamp failure must never break construction
        _log.warning(
            "could not attach guards to a %s; they will run but will not appear "
            "on the registered manifest",
            type(agent).__name__,
        )
        return agent
    _preserve_stamp_across_clone(agent, stamp)
    return agent


def _preserve_stamp_across_clone(agent: object, guards: "tuple[Guard, ...]") -> None:
    """Make the stamp survive ``agent.clone()`` (OpenAI Agents).

    The stamp is a plain instance attribute, so ``Agent.clone()`` / ``dataclasses.replace``
    rebuild from the declared fields and drop it — and since the runtime now reads the
    stamp, a cloned agent would silently run **unguarded**. Wrap ``clone`` so a clone
    re-stamps (recursively). Only agents that expose ``clone`` (OpenAI) are touched;
    best-effort — a failure to override leaves cloning as-is rather than raising.
    """
    original = getattr(agent, "clone", None)
    if not callable(original):
        return

    def _clone(*args: object, **kwargs: object) -> object:
        return attach_guards(original(*args, **kwargs), guards)

    try:
        agent.clone = _clone  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def resolve_guards(
    agent: object, explicit: "Sequence[Guard] | None"
) -> "Sequence[Guard] | None":
    """The guard list to use: the ``explicit`` one when given, else the agent's stamp.

    The single rule shared by the manifest view (:func:`~hexgate.manifest.create_manifest`)
    and every runtime seam (the adapters' wrappers/runners), so a guard the agent is
    stamped with is both declared on its manifest and actually run — the two must agree.
    An explicit ``[]`` overrides the stamp to "no guards"; ``None`` falls back to it.
    """
    return explicit if explicit is not None else read_guards(agent)


def read_guards(agent: object) -> "tuple[Guard, ...] | None":
    """Return guards previously stamped by :func:`attach_guards`, or ``None``.

    ``None`` for both an unstamped agent and an empty stamp, so a caller can
    treat "no guards" uniformly.
    """
    guards = getattr(agent, GUARDS_ATTR, None)
    return tuple(guards) if guards else None
