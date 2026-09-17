"""First-class sub-agent edges for a native :class:`HexgateAgent`.

A parent nests children with ``create_agent(..., subagents=[child])`` — or the
explicit ``as_tool(child)`` / ``as_handoff(child)`` forms. ``as_tool`` mounts an
**agent-as-tool** edge: a policy-gated delegation tool that decides
``agent.tool:<child>`` under the parent's policy, then runs the child's own
enforced ``ainvoke`` (the child governs itself, and the caller's role rides the
ambient context in). ``as_handoff`` (control transfer) is **not** supported on the
native / pydantic frameworks — a real handoff means composing the child's graph
into the parent's — so it raises :class:`UnsupportedReach`.

The two ``via`` modes are independent policy grants (``agent.tool:`` vs
``agent.handoff:``); see ``hexgate/security/models.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hexgate.security.models import AgentVia

if TYPE_CHECKING:
    # Runtime import would be circular (factory -> subagents -> factory); the
    # edge only needs the type for annotations.
    from hexgate.agents.factory import HexgateAgent


class UnsupportedReach(RuntimeError):
    """A reach mode the current framework can't execute.

    Raised by ``as_handoff`` on a native / pydantic agent: those expose no
    control-transfer seam, so a handoff edge would be a silent no-op. Use
    ``as_tool`` (agent-as-tool) instead, or an OpenAI/Google agent for handoff.
    """


@dataclass(frozen=True)
class SubagentEdge:
    """A parent→child delegation edge.

    ``via`` selects the reach mode — ``"tool"`` (agent-as-tool: the child runs and
    control *returns* to the caller) or ``"handoff"`` (control *transfers* to the
    child). ``as_name`` overrides the LLM-facing delegation tool name (defaults to
    ``delegate_to_<child>``).
    """

    child: "HexgateAgent"
    via: AgentVia = "tool"
    as_name: str | None = None


def as_tool(child: "HexgateAgent", *, as_name: str | None = None) -> SubagentEdge:
    """Mount ``child`` as an agent-as-tool sub-agent (gated ``agent.tool:<name>``)."""
    return SubagentEdge(child=child, via="tool", as_name=as_name)


def as_handoff(child: "HexgateAgent", *, as_name: str | None = None) -> SubagentEdge:
    """Mount ``child`` via handoff (control transfer).

    Unsupported on native / pydantic agents — raises :class:`UnsupportedReach` when
    mounted there.
    """
    return SubagentEdge(child=child, via="handoff", as_name=as_name)
