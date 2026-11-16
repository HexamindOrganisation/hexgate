"""Risk facts: allowed calls whose risky argument the composed policy leaves open.

Read from one role's effective policy (after inheritance, modules and defaults)
and the agent's manifest: what the policy allows, not a lint. Two kinds:

- ``unbounded_number``: an argument the manifest types as a number that nothing
  caps from above (an optional number the manifest records as a string is missed);
- ``destructive_without_approval``: a tool whose name holds a destructive verb,
  allowed with no approval step. A guess from the name.

Only ``allow`` calls count: an ``approval_required`` one has a person deciding
it. Other constraints don't clear a fact (``currency == "EUR"`` alone still
allows any ``amount``); ``constraints_present`` lists them.
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from hexgate.manifest.models import AgentManifest, InputSchema
    from hexgate.security import AgentPolicy
    from hexgate.security.constraints import Cmp, Node, Operand

FactKind = Literal["unbounded_number", "destructive_without_approval"]

_NUMBER_TYPES = frozenset({"number", "integer"})
_DESTRUCTIVE_VERBS = frozenset(
    {
        "delete", "remove", "drop", "destroy", "purge", "truncate", "wipe",
        "erase", "revoke", "terminate", "kill", "reset", "overwrite",
    }
)  # fmt: skip
# Operators that bound an arg from above, with the arg on the left; a comparison
# with the arg on the right is mirrored first (``500 >= args.amount``).
_UPPER = frozenset({"<", "<=", "==", "in"})
_MIRROR = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "==": "=="}


@dataclass(frozen=True)
class _Call:
    """The call a constraint is read for: its tool and role. In the default
    bucket, ``named_roles`` are the roles the policy defines, which none of
    its callers holds; None in a named role."""

    tool: str
    role: str
    named_roles: frozenset[str] | None = None


def facts_for(
    agent: str,
    role: str,
    policy: AgentPolicy,
    manifest: AgentManifest | None,
    roles: Collection[str],
) -> list[dict]:
    """Every risk fact for one agent in one role, in tool order. ``roles`` are
    the policy's roles, which a caller in the default bucket doesn't hold."""
    from hexgate.security import DEFAULT_ROLE_NAME
    from hexgate.security.constraints import parse_constraint
    from hexgate.security.policy import get_tool_policy

    schemas = {t.name: t.input_schema for t in manifest.tools} if manifest else {}
    facts: list[dict] = []
    for tool in _tools(policy, manifest):
        rule = get_tool_policy(policy, tool)
        if rule.mode != "allow":
            continue
        present = [*policy.constraints, *rule.constraints]
        nodes = [parse_constraint(source) for source in present]
        named = frozenset(roles) - {DEFAULT_ROLE_NAME}
        # The default bucket serves every caller with no named role.
        call = _Call(tool, role, named if role == DEFAULT_ROLE_NAME else None)
        if any(_unpassable(node, call) for node in nodes):
            continue  # no call of this tool in this role passes, so no kind applies
        facts += [
            dict(agent=agent, role=role, tool=tool, kind=kind, argument=argument,
                 constraints_present=present)
            for kind, argument in _tool_risks(tool, schemas, nodes, policy.consts, call)
        ]  # fmt: skip
    return facts


def _tools(policy: AgentPolicy, manifest: AgentManifest | None) -> list[str]:
    """The tools the agent may call: its manifest's, or without one every
    authored rule. Agent and skill keys are reach, not calls, so they're left
    out."""
    from hexgate.security.models import is_reserved_key

    names = [t.name for t in manifest.tools] if manifest else policy.effective_tools
    return sorted({name for name in names if not is_reserved_key(name)})


def _tool_risks(
    tool: str,
    schemas: dict[str, InputSchema],
    nodes: list[Node],
    consts: dict,
    call: _Call,
) -> Iterator[tuple[FactKind, str | None]]:
    if _is_destructive(tool):
        yield "destructive_without_approval", None
    props = schemas[tool].properties if tool in schemas else {}
    for name, prop in sorted(props.items()):
        if prop.type in _NUMBER_TYPES:
            if not any(_caps(node, name, consts, call) for node in nodes):
                yield "unbounded_number", name


def _is_destructive(tool: str) -> bool:
    """Whether a word of the tool's name is a destructive verb."""
    return any(word in _DESTRUCTIVE_VERBS for word in _words(tool))


_WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def _words(name: str) -> list[str]:
    """``name`` split on separators, camelCase and digits, lowercased
    (``db.drop_table`` → ``db drop table``, ``HTTPDelete`` → ``http delete``,
    ``getADelete`` → ``get a delete``, ``delete2fa`` → ``delete 2 fa``)."""
    return [w.lower() for w in _WORD.findall(name)]


def _caps(node: Node, arg: str, consts: dict, call: _Call) -> bool:
    """Whether every call that passes ``node`` has ``args.<arg>`` capped from
    above by a number, literal or const. A string bound caps nothing, since
    strings compare as text (``"1000000" <= "500"`` holds), so neither does a
    ctx or run field, whose type the policy doesn't fix."""
    from hexgate.security.constraints import Cmp, ConstRef, Lit

    def numeric(bound: Operand) -> bool:
        value = (
            bound.value if isinstance(bound, Lit)
            else consts.get(bound.name) if isinstance(bound, ConstRef)
            else None
        )  # fmt: skip
        values = value if isinstance(value, list) else [value]
        return all(_finite_number(v) for v in values)

    def leaf_caps(leaf: Node) -> bool:
        op, bound = _bound(leaf, arg) if isinstance(leaf, Cmp) else (None, None)
        return op in _UPPER and numeric(bound)

    return _holds(node, leaf_caps, call)


def _finite_number(value: object) -> bool:
    """An int (one too large for a float still caps) or a finite float: inf
    and nan cap nothing, and a bool isn't a number."""
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, int) and not isinstance(value, bool)


def _unpassable(node: Node, call: _Call) -> bool:
    """Whether no call passes ``node``: the same walk as :func:`_holds` with a
    leaf that never holds, so only the parts decided False count."""
    return _holds(node, lambda _leaf: False, call)


def _bound(cmp: Cmp, arg: str) -> tuple[str | None, Operand | None]:
    """The operator and value ``cmp`` holds ``args.<arg>`` to, read with the
    arg on the left (``500 >= args.amount`` → ``<=``); Nones otherwise."""
    left, op, right = cmp.left, cmp.op, cmp.right
    if _is_arg(right, arg) and op in _MIRROR:
        left, op, right = right, _MIRROR[op], left
    return (op, right) if _is_arg(left, arg) else (None, None)


def _holds(node: Node, leaf, call: _Call) -> bool:
    """Whether ``leaf`` holds on every path through ``node``: one part of an
    And is enough, every branch of an Or is needed. A part that reads only the
    call's tool and role is decided: one no call passes bounds vacuously, one
    every call passes bounds nothing (``tool != "x" or args.amount <= 5``). A Not
    never counts: a comparison on a missing or mistyped value is False, so its
    negation passes it. Nor does a quantifier, which binds elements."""
    from hexgate.security.constraints import And, Cmp, Or

    decided = _decided(node, call)
    if decided is not None:
        return not decided
    if isinstance(node, Cmp):
        return leaf(node)
    if isinstance(node, (And, Or)):
        results = (_holds(part, leaf, call) for part in node.parts)
        return all(results) if isinstance(node, Or) else any(results)
    return False


def _decided(node: Node, call: _Call) -> bool | None:
    """``node``'s result when it reads only the tool and role, else None. In
    the default bucket a role test is decided only when it names roles the
    policy defines (``role == "admin"``), which no caller there holds."""
    from hexgate.security.constraints import (
        evaluate_constraint,
        iter_arg_refs,
        iter_const_refs,
    )

    paths = set(iter_arg_refs(node))
    if not paths or not paths <= {("tool",), ("role",)} or any(iter_const_refs(node)):
        return None
    role: str | None = call.role
    if call.named_roles is not None and ("role",) in paths:
        tested = _role_literals(node)
        if tested is None or not tested <= call.named_roles:
            return None
        role = None  # holds none of them: tests as no role
    return evaluate_constraint(node, {"tool": call.tool, "role": role})


def _role_literals(node: Node) -> set[str] | None:
    """The role names ``node`` tests with ``role ==`` / ``role in``, or None
    if it reads the role any other way."""
    from hexgate.security.constraints import And, Cmp, Lit, Not, Or, Ref, iter_arg_refs

    if isinstance(node, (And, Or, Not)):
        found: set[str] = set()
        for part in node.parts if isinstance(node, (And, Or)) else (node.inner,):
            got = _role_literals(part)
            if got is None:
                return None
            found |= got
        return found
    if isinstance(node, Cmp) and node.left == Ref(("role",)):
        value = node.right.value if isinstance(node.right, Lit) else None
        values = value if isinstance(value, list) else [value]
        if node.op in ("==", "in") and all(isinstance(v, str) for v in values):
            return set(values)
        return None
    return None if ("role",) in set(iter_arg_refs(node)) else set()


def _is_arg(operand: Operand, arg: str) -> bool:
    from hexgate.security.constraints import Ref

    return isinstance(operand, Ref) and operand.path == ("args", arg)
