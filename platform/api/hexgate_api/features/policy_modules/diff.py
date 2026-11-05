"""What a set of draft files changes, per (agent, role, tool), before saving.

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

# The agent-wide fields compose injects onto every role besides its tools.
ROLE_KEYS = ("guards", "usage")
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


def _field_changes(agent: str, role: str, was: dict, now: dict) -> list[dict]:
    """The agent-wide fields that differ between two serialized roles."""
    return [
        {"agent": agent, "role": role, "key": key}
        | {"before": was.get(key), "after": now.get(key)}
        for key in ROLE_KEYS
        if was.get(key) != now.get(key)
    ]


def diff_files(saved: Mapping[str, str], drafted: Mapping[str, str]) -> dict:
    """Compare the saved project with the drafted one. When either side does not
    compose there is nothing to compare: ``changes`` is empty and ``composes``
    says which side failed (``/policy/check`` reports why)."""
    from hexgate.security import DEFAULT_ROLE_NAME

    old = _resolve(saved)
    new = old if drafted == saved else _resolve(drafted)
    changes: list[dict] = []
    role_changes: list[dict] = []
    gate_changes: list[dict] = []
    if old is not None and new is not None and new is not old:
        for agent in sorted(set(old) | set(new)):
            a, b = _resolution_for(old, agent), _resolution_for(new, agent)
            agent_keys = _agent_keys(a, b)
            gates_a, gates_b = _gates(a), _gates(b)
            gate_changes += [
                {"agent": agent, "gate": g, "before": gates_a[g], "after": gates_b[g]}
                for g in GATES
                if gates_a[g] != gates_b[g]
            ]
            side_a = (a, _open_keys(gates_a, agent_keys))
            side_b = (b, _open_keys(gates_b, agent_keys))
            shown = _shown(gates_a, gates_b)
            # The serializer /policy/resolve uses, so a value reads the same there.
            json_a, json_b = roles_json(a), roles_json(b)
            for role in sorted(set(a.by_role) | set(b.by_role)):
                changes += _tool_changes(agent, role, side_a, side_b, shown)
                # A role one side lacks falls back to its `default`, as at runtime.
                was = json_a.get(role) or json_a[DEFAULT_ROLE_NAME]
                now = json_b.get(role) or json_b[DEFAULT_ROLE_NAME]
                role_changes += _field_changes(agent, role, was, now)
    return {
        "changes": changes,
        "role_changes": role_changes,
        "gate_changes": gate_changes,
        "agents_affected": sorted(
            {c["agent"] for c in changes + role_changes + gate_changes}
        ),
        "composes": {"before": old is not None, "after": new is not None},
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
