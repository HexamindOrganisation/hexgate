"""What a set of draft files would change compared with the saved policy files:
per (agent, role, tool) grant, per agent for guards, usage and the opt-in gates.

Both sides are the same compose resolution the editor and the compile path use;
this module only compares them. Only :func:`diff_project` reads the store.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.features.policy_modules.service import (
    ENTRY_FILE,
    InvalidModuleError,
    _files_map,
    _resolution_for,
    _resolve_all_agents_files,
    compose_error_types,
    overlay_drafts,
    roles_json,
)
from hexgate_api.schemas import PolicyFileDraft

# The agent-level fields compose puts, identically, on every role of an agent.
AGENT_FIELDS = ("guards", "usage")
# An agent gate no role declares is off: the runtime lets every caller through.
OPEN = {"mode": "allow", "constraints": []}
# The opt-in gates, each on only while some role lists a key of its kind, and the
# PolicySet method that says so.
GATES = {
    "admission": "declares_admission",
    "reach": "declares_reach",
    "tool_reach": "declares_tool_reach",
    "skills": "declares_skills",
}


def _resolve(files: Mapping[str, str]) -> dict | None:
    """Every declared agent's resolution, or None when the files don't compose."""
    if ENTRY_FILE not in files:
        return None
    try:
        return _resolve_all_agents_files(dict(files))
    except compose_error_types():
        return None


def _agent_keys(*results) -> set[str]:
    """Every admission / reach key any role of either side lists, deny included,
    with both transfer modes of each reach target."""
    from hexgate.security.models import is_agent_key

    keys = {
        key
        for result in results
        for role in result.policy_set.roles
        for key in result.policy_set.policy_for(role).effective_tools
        if is_agent_key(key)
    }
    return keys.union(*_reach_pairs(keys).values())


def _gates(result) -> dict[str, bool]:
    """Which opt-in gates are on in one agent's resolution."""
    return {gate: getattr(result.policy_set, m)() for gate, m in GATES.items()}


def _open_keys(gates: dict[str, bool], agent_keys: set[str]) -> set[str]:
    """The agent keys a side lets every caller through: admission and handoff
    when their gate is off. (The as-tool and skill seams fall back to the tool's
    own name instead, which no key can show; see :func:`_shown`.)"""
    from hexgate.security.models import AGENT_RUN_TOOL, is_agent_via_key

    off = set()
    if not gates["admission"]:
        off |= {AGENT_RUN_TOOL} & agent_keys
    if not gates["reach"]:
        off |= {k for k in agent_keys if is_agent_via_key(k, "handoff")}
    return off


def _shown(gates_a: dict[str, bool], gates_b: dict[str, bool]):
    """Whether a key has a value on both sides. An as-tool or skill key whose gate
    is off on a side has none there; that gate's flip is a gate change instead."""
    from hexgate.security.models import is_agent_via_key, is_skill_key

    def both(gate: str) -> bool:
        return gates_a[gate] and gates_b[gate]

    def shown(key: str) -> bool:
        if is_agent_via_key(key, "tool"):
            return both("tool_reach")
        return both("skills") if is_skill_key(key) else True

    return shown


def _grants(result, role: str, open_keys: set[str]) -> dict[str, dict]:
    """What a caller in ``role`` may do on one side, as the runtime decides it: an
    undefined role gets the ``default`` policy, a deny reads as unreachable like a
    key the role never lists, and an agent key whose gate is off reads as open."""
    grants = {
        key: {"mode": rule.mode, "constraints": list(rule.constraints)}
        for key, rule in result.policy_set.policy_for(role).effective_tools.items()
        if rule.mode != "deny"
    }
    return grants | dict.fromkeys(open_keys, OPEN)


def _reach_pairs(keys: set[str]) -> dict[str, tuple[str, str]]:
    """Each reach target → its two SDK keys (``agent.tool:`` / ``agent.handoff:``)."""
    from hexgate.security.models import agent_reach_target, agent_target_key

    targets = {agent_reach_target(k) for k in keys} - {None}
    return {
        t: (agent_target_key("tool", t), agent_target_key("handoff", t))
        for t in targets
    }


def _wire_keys(before: dict, after: dict) -> dict[str, str]:
    """The wire ``tool`` name for each SDK key that changed. Both reach vias of a
    target collapse to ``agent:<name>`` when they agree on both sides; when they
    differ, each keeps its own SDK key so no change is hidden."""
    keys = set(before) | set(after)
    changed = {k for k in keys if before.get(k) != after.get(k)}
    wire: dict[str, str] = {}
    for target, (via_tool, via_handoff) in _reach_pairs(keys).items():

        def same(side: dict) -> bool:
            return side.get(via_tool) == side.get(via_handoff)

        if same(before) and same(after) and via_tool in changed:
            wire[f"agent:{target}"] = via_tool
            changed -= {via_tool, via_handoff}
    for key in changed:
        wire[key] = key
    return wire


def _change(agent: str, role: str, tool: str, before, after) -> dict:
    was = before["constraints"] if before else []
    now = after["constraints"] if after else []
    return {
        "agent": agent,
        "role": role,
        "tool": tool,
        "before": before,
        "after": after,
        "constraints_added": [c for c in now if c not in was],
        "constraints_removed": [c for c in was if c not in now],
    }


def _tool_changes(agent: str, role: str, side_a, side_b, shown) -> list[dict]:
    """The (agent, role, tool) triples whose grant differs between two sides, each
    a ``(resolution, open_keys)`` pair. Keys with no value on a side are dropped
    before the reach vias collapse, so a hidden via never hides its sibling."""
    before, after = (
        {k: v for k, v in _grants(result, role, open_keys).items() if shown(k)}
        for result, open_keys in (side_a, side_b)
    )
    return [
        _change(agent, role, tool, before.get(sdk), after.get(sdk))
        for tool, sdk in sorted(_wire_keys(before, after).items())
    ]


def _field_changes(agent: str, a, b) -> list[dict]:
    """The agent-level fields that differ between two resolutions. Compose puts
    the same value on every role, so the ``default`` role speaks for the agent;
    it is read through the serializer /policy/resolve uses, so a value reads the
    same there."""
    from hexgate.security import DEFAULT_ROLE_NAME

    was = roles_json(a, role=DEFAULT_ROLE_NAME)[DEFAULT_ROLE_NAME]
    now = roles_json(b, role=DEFAULT_ROLE_NAME)[DEFAULT_ROLE_NAME]
    return [
        {"agent": agent, "key": key, "before": was.get(key), "after": now.get(key)}
        for key in AGENT_FIELDS
        if was.get(key) != now.get(key)
    ]


def _gate_changes(agent: str, gates: tuple[dict, dict]) -> list[dict]:
    """The opt-in gates one agent's drafts switch on or off."""
    gates_a, gates_b = gates
    return [
        {"agent": agent, "gate": g, "before": gates_a[g], "after": gates_b[g]}
        for g in GATES
        if gates_a[g] != gates_b[g]
    ]


def _diff_agent_tools(agent: str, a, b, gates: tuple[dict, dict]) -> list[dict]:
    """One agent's tool changes, every role on either side."""
    gates_a, gates_b = gates
    agent_keys = _agent_keys(a, b)
    side_a = (a, _open_keys(gates_a, agent_keys))
    side_b = (b, _open_keys(gates_b, agent_keys))
    shown = _shown(gates_a, gates_b)
    return [
        change
        for role in sorted(set(a.by_role) | set(b.by_role))
        for change in _tool_changes(agent, role, side_a, side_b, shown)
    ]


def diff_files(saved: Mapping[str, str], drafted: Mapping[str, str]) -> dict:
    """Compare the saved project with the drafted one. When either side does not
    compose there is nothing to compare: ``changes`` is empty and ``composes``
    says which side failed (``/policy/check`` reports why)."""
    old = _resolve(saved)
    new = old if drafted == saved else _resolve(drafted)
    composes = {"before": old is not None, "after": new is not None}
    if old is None or new is None or new is old:
        return _response([], [], [], composes)
    changes, agent_changes, gate_changes = [], [], []
    for agent in sorted(set(old) | set(new)):
        a, b = _resolution_for(old, agent), _resolution_for(new, agent)
        gates = _gates(a), _gates(b)
        changes += _diff_agent_tools(agent, a, b, gates)
        agent_changes += _field_changes(agent, a, b)
        gate_changes += _gate_changes(agent, gates)
    return _response(changes, agent_changes, gate_changes, composes)


def _response(changes, agent_changes, gate_changes, composes) -> dict:
    """The wire dict; ``agents_affected`` is every agent the three lists name."""
    touched = {c["agent"] for c in changes + agent_changes + gate_changes}
    return {
        "changes": changes,
        "agent_changes": agent_changes,
        "gate_changes": gate_changes,
        "agents_affected": sorted(touched),
        "composes": composes,
    }


async def diff_project(
    session: AsyncSession, project_id: str, drafts: Iterable[PolicyFileDraft]
) -> dict:
    """:func:`diff_files` of the stored files against them with ``drafts`` applied.
    Raises :class:`~service.InvalidModuleError` when no ``policy.yaml`` is stored
    (so ``overlay_drafts`` never sees a project it would refuse):
    such a project enforces from the module store or each agent's own policy, so
    there is no saved side to compare, and a first ``policy.yaml`` replaces it."""
    saved = await _files_map(session, project_id)
    if ENTRY_FILE not in saved:
        raise InvalidModuleError(
            f"this project has no {ENTRY_FILE!r}, so it does not enforce from policy "
            f"files and there is nothing to diff; saving a first {ENTRY_FILE!r} "
            "would switch every agent onto the file store"
        )
    # One read: both sides come from the same snapshot of the store.
    return diff_files(saved, overlay_drafts(saved, drafts))
