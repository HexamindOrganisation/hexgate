"""The names a policy uses that the MCP would never have shown the agent:
tools, skills, guards, arguments and caller attributes it invents.

These checks own invented names, so `policy.py` runs `analyze_policy` on a
single-file policy without a manifest: its drift and `unknown-guard` lints would
fail `valid` on the same names. A module tree's `check_project` gets the
manifests (`load_project_agents`) for `unknown-agent` and `unknown-reach-target`,
which also turns on its drift lints. Those see a boundary ceiling on an invented
tool, which linking drops before these checks run (it keeps an unconditional
deny), but only when every registered agent has a manifest: otherwise such a
typo passes, leaving the real tool uncapped (left to PR 17's module scanning).
A module tree's `"*"` cells and kept boundary denies may name any agent's tools,
so its names are every agent's (`load_project_names`); `check_project` checks
the case agent's named column, if it has one, against its own manifest.
"""

from __future__ import annotations

from collections.abc import Iterator

from evals.policy_writing.sources import KnownNames
from hexgate.security.constraints import iter_arg_refs, parse_constraint
from hexgate.security.models import (
    REACH_ARGS,
    SKILL_SCRIPT_ARGS,
    AgentPolicy,
    gate_args,
    is_reserved_key,
    is_skill_key,
    skill_key,
)
from hexgate.security.naming import canonical_skill_name
from hexgate.security.network import EGRESS_TOOL_ARGS
from hexgate.security.policy_set import PolicySet

# What a constraint path may start with: anything else parses, then never matches.
# Loading already rejects an unknown `run.*` path.
ROOTS = {"args", "ctx", "run", "role", "tool"}


def _roles(policy_set: PolicySet) -> list[AgentPolicy]:
    """Each role as it is enforced: inheritance and file-level constraints merged."""
    return [policy_set.policy_for(role) for role in policy_set.roles]


def unknown_keys(policy_set: PolicySet, known: KnownNames) -> list[str]:
    """Tools, skills (`skill:<name>`) and guards (`guard:<name>`) a policy keys on
    that `known` doesn't list. Synthetic tool keys are always known here: the
    `<target>` of `agent.<via>:<target>` is checked against `agents.json` only in
    a module tree, by `check_project`'s `unknown-reach-target` (`policy.py`); a
    single-file policy's is not checked."""
    bad = set()
    for p in _roles(policy_set):
        bad |= {
            t
            for t in p.tools
            if t not in known.tools
            and t not in EGRESS_TOOL_ARGS
            and not is_reserved_key(t)
        }
        # Trimmed as the runtime trims them, so ` pdf ` governs the skill `pdf`.
        # A module tree's skills arrive lowered into `tools` (`skill.script:pdf`).
        skills = {canonical_skill_name(s) for s in p.skills}
        skills |= {t.split(":", 1)[1] for t in p.tools if is_skill_key(t)}
        bad |= {skill_key("instructions", s) for s in skills - known.skills}
        bad |= {f"guard:{g}" for g in p.guards.keys() - known.guards}
    return sorted(bad)


def _constraint_lines(p: AgentPolicy) -> Iterator[tuple[str | None, str]]:
    """(tool key the constraint applies to, or None for a policy-level one that
    may meet any call; its text)."""
    for c in [*p.constraints, *p.default_policy.constraints]:
        yield None, c
    for key, tool in p.effective_tools.items():
        yield from ((key, c) for c in tool.constraints)


def _allowed_args(
    tool: str | None, tools: dict[str, set[str]], every_arg: set[str]
) -> set[str] | None:
    """The `args.*` names a call under `tool` carries; None for an unknown tool."""
    if tool is None:
        return every_arg
    if is_reserved_key(tool):  # agent.run, agent.<via>:<target>, skill*:<name>
        return gate_args(tool)
    if tool in EGRESS_TOOL_ARGS:
        return EGRESS_TOOL_ARGS[tool]
    return tools.get(tool)


def _path_ok(path: tuple[str, ...], allowed: set[str] | None, attrs: set[str]) -> bool:
    kind, name = path[0], path[1] if len(path) > 1 else ""
    if kind not in ROOTS:
        return False
    if kind in ("role", "tool"):
        return not name  # a plain string: `role.x` never matches
    if kind == "run" or not name:
        return True
    if kind == "ctx":
        return name in attrs
    return allowed is None or name in allowed  # an unknown tool fails elsewhere


def unknown_refs(policy_set: PolicySet, known: KnownNames) -> list[str]:
    """Constraint paths that read a name the manifest or audit rows don't define."""
    every_arg = set().union(
        *known.tools.values(),
        *EGRESS_TOOL_ARGS.values(),
        REACH_ARGS,  # admission's `agent` too
        SKILL_SCRIPT_ARGS,  # every skill level's too
    )
    bad = set()
    for p in _roles(policy_set):
        for tool, text in _constraint_lines(p):
            allowed = _allowed_args(tool, known.tools, every_arg)
            bad |= {
                f"{tool or 'policy-level'}: {'.'.join(path)}"
                for path in iter_arg_refs(parse_constraint(text))
                if not _path_ok(path, allowed, known.attrs)
            }
    return sorted(bad)
