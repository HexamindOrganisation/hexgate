"""Sub-agent edge type for a native :class:`HexgateAgent`.

Native reach is **agent-as-tool** only: ``parent = create_agent(tools=[child.as_tool()])``.
The child runs and control *returns* to the caller; :meth:`HexgateAgent.as_tool` mounts a
policy-gated delegation tool that decides ``agent.tool:<child>`` under the parent's policy,
then runs the child's own enforced ``ainvoke``. Handoff (control transfer) has no native
seam and is not offered on native/pydantic — it exists only on OpenAI/Google agents,
authored with their own SDKs and read/gated by hexgate.

``SubagentEdge`` is the internal representation the enumerable view
:meth:`HexgateAgent.subagents` yields, keyed by ``via`` (``agent.tool:`` /
``agent.handoff:`` — see ``hexgate/security/models.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hexgate.security.models import AgentVia

if TYPE_CHECKING:
    # Runtime import would be circular (factory -> subagents -> factory); the
    # edge only needs the type for annotations.
    from hexgate.agents.factory import HexgateAgent


@dataclass(frozen=True)
class SubagentEdge:
    """A resolved parent→child delegation edge (the enumerable view's unit).

    ``via`` is the reach mode — ``"tool"`` (agent-as-tool: the child runs and control
    *returns*) or ``"handoff"`` (control *transfers*, OpenAI/Google only). ``as_name`` is
    the LLM-facing delegation tool name for a tool edge (``delegate_to_<child>`` by default).
    """

    child: "HexgateAgent"
    via: AgentVia = "tool"
    as_name: str | None = None
