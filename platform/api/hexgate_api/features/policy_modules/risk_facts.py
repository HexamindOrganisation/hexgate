"""Risk facts: allowed calls whose risky argument the composed policy leaves open.

Read from one role's effective policy (after inheritance, modules and defaults)
and the agent's manifest: what the policy allows, not a lint. Three kinds:

- ``unbounded_number``: an argument the manifest types as a number that nothing
  caps from above (an optional number the manifest records as a string is missed);
- ``host_without_allowlist``: a URL or host argument, or HTTP egress, that no
  constraint pins to a named host. Which arguments are hosts is a guess from
  the names of top-level string arguments (a URL inside an object is missed);
- ``destructive_without_approval``: a tool whose name holds a destructive verb,
  allowed with no approval step. A guess from the name.

Only ``allow`` calls count: an ``approval_required`` one has a person deciding
it. Other constraints don't clear a fact (``currency == "EUR"`` alone still
allows any ``amount``); ``constraints_present`` lists them.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterator
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from hexgate.manifest.models import AgentManifest, InputSchema
    from hexgate.security import AgentPolicy
    from hexgate.security.constraints import Cmp, Node, Operand

FactKind = Literal[
    "unbounded_number", "host_without_allowlist", "destructive_without_approval"
]

_NUMBER_TYPES = frozenset({"number", "integer"})
_HOST_WORDS = ("host", "hostname", "domain", "url", "uri", "endpoint", "webhook")
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
# A prefix that fixes the host: scheme, then the whole authority up to its "/".
_HOST_PREFIX = re.compile(r"^[a-z][a-z0-9+.-]*://[^/]+/", re.I)
# A suffix that names a domain from its dot (``.example.com``, ``.internal``);
# its last label has a letter, so an IP's ``.1`` names nothing.
_DOMAIN_SUFFIX = re.compile(r"^(\.[a-z0-9-]+)*\.[a-z0-9-]*[a-z][a-z0-9-]*$", re.I)
# ``_HOST_PREFIX`` as an anchored RE2 pattern: a literal authority (dots escaped), and
# whose "/" is not made optional (``/?`` lets ``api.x.com.evil.net`` through).
_LITERAL_HOST_PATTERN = re.compile(
    r"^\^[a-z][a-z0-9-]*://(?:[a-z0-9@:-]|\\\.)+/(?![?*+{])", re.I
)


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
    for tool in _tools(policy, schemas):
        rule = get_tool_policy(policy, tool)
        if rule.mode != "allow":
            continue
        present = [*policy.constraints, *rule.constraints]
        nodes = [parse_constraint(source) for source in present]
        known = {"tool": tool, "role": role}
        if role == DEFAULT_ROLE_NAME:
            # The default bucket serves every caller with no named role.
            known["named_roles"] = frozenset(roles) - {DEFAULT_ROLE_NAME}
        if any(_decided(node, known) is False for node in nodes):
            continue  # a constraint no call of this tool in this role passes
        facts += [
            dict(agent=agent, role=role, tool=tool, kind=kind, argument=argument,
                 constraints_present=present)
            for kind, argument in _tool_risks(tool, schemas, nodes, policy.consts, known)
        ]  # fmt: skip
    return facts


def _tools(policy: AgentPolicy, schemas: dict[str, InputSchema]) -> list[str]:
    """The tools the agent may call: its manifest's, every authored rule, and
    HTTP egress, which a permissive default opens even when no rule names it.
    Agent and skill keys are reach, not calls, so they're left out."""
    from hexgate.security.models import is_reserved_key
    from hexgate.security.network import NET_HTTP_REQUEST

    names = set(policy.effective_tools) | set(schemas) | {NET_HTTP_REQUEST}
    return sorted(name for name in names if not is_reserved_key(name))


def _tool_risks(
    tool: str,
    schemas: dict[str, InputSchema],
    nodes: list[Node],
    consts: dict,
    known: dict,
) -> Iterator[tuple[FactKind, str | None]]:
    from hexgate.security.network import NET_HTTP_REQUEST

    if _is_destructive(tool):
        yield "destructive_without_approval", None
    # HTTP egress only: a TCP egress target is fixed in code (tcp_egress_guard).
    if tool == NET_HTTP_REQUEST:
        # A request is pinned by its host or its url. Only the host is parsed;
        # the url is the request's own URI, so a suffix doesn't pin it. (A url
        # pin covers plain HTTP: CONNECT's url has no path, so HTTPS is denied.)
        pinned = ("host", "url")
        if not any(
            _pins(node, arg, known, parsed=arg == "host")
            for node in nodes
            for arg in pinned
        ):
            yield "host_without_allowlist", "host"
        return
    props = schemas[tool].properties if tool in schemas else {}
    for name, prop in sorted(props.items()):
        if prop.type in _NUMBER_TYPES:
            if not any(_caps(node, name, consts, known) for node in nodes):
                yield "unbounded_number", name
        elif prop.type == "string" and _is_host_name(name):
            if not any(_pins(node, name, known) for node in nodes):
                yield "host_without_allowlist", name


def _is_host_name(name: str) -> bool:
    """Whether a word of the argument's name names a host, singular or plural
    (``baseUrl``, ``host_name``, ``allowed_hosts``), or its run-together end does
    (``targeturl``, ``targetURLs``, ``url2``)."""
    words = [word.rstrip("0123456789").removesuffix("s") for word in _words(name)]
    run_together = name.lower().rstrip("0123456789").removesuffix("s")
    return any(word in _HOST_WORDS for word in words) or run_together.endswith(
        _HOST_WORDS
    )


def _is_destructive(tool: str) -> bool:
    """Whether a word of the tool's name is a destructive verb."""
    return any(word in _DESTRUCTIVE_VERBS for word in _words(tool))


def _words(name: str) -> list[str]:
    """``name`` split on separators and camelCase, lowercased
    (``db.drop_table`` → ``db drop table``, ``HTTPDelete`` → ``http delete``)."""
    spaced = re.sub(r"([a-z0-9])([A-Z])|([A-Z])([A-Z][a-z])", r"\1\3 \2\4", name)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]


def _caps(node: Node, arg: str, consts: dict, known: dict) -> bool:
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
        return all(
            isinstance(v, int | float) and not isinstance(v, bool) for v in values
        )

    def leaf_caps(leaf: Node) -> bool:
        op, bound = _bound(leaf, arg) if isinstance(leaf, Cmp) else (None, None)
        return op in _UPPER and numeric(bound)

    return _holds(node, leaf_caps, known)


def _pins(node: Node, arg: str, known: dict, parsed: bool = False) -> bool:
    """Whether every call that passes ``node`` has ``args.<arg>`` pinned to a
    named host: ``==`` / ``in`` a fixed value, or a ``startswith`` or anchored
    ``matches`` whose literal text runs past the host to its ``/``. A suffix
    (``endswith(args.host, ".example.com")``) pins only a ``parsed`` host, the
    one the egress proxy reads off the request: a raw string the model writes
    can end in it after a ``/`` or ``?``. A scheme-only prefix pins nothing."""
    from hexgate.security.constraints import Call, Cmp

    def leaf_pins(leaf: Node) -> bool:
        if isinstance(leaf, Cmp):
            op, bound = _bound(leaf, arg)
            return op in ("==", "in") and _is_fixed(bound)
        if not isinstance(leaf, Call) or not _is_arg(leaf.arg, arg):
            return False
        value = leaf.value.value
        if leaf.fn == "startswith":
            return bool(_HOST_PREFIX.match(value))
        if leaf.fn == "matches":
            return _literal_host_prefix(value)
        return leaf.fn == "endswith" and parsed and bool(_DOMAIN_SUFFIX.match(value))

    return _holds(node, leaf_pins, known)


def _literal_host_prefix(pattern: str) -> bool:
    """Whether an RE2 ``pattern`` is anchored and its text up to the host's ``/``
    is literal, with no alternation anywhere."""
    return "|" not in pattern and bool(_LITERAL_HOST_PATTERN.match(pattern))


def _bound(cmp: Cmp, arg: str) -> tuple[str | None, Operand | None]:
    """The operator and value ``cmp`` holds ``args.<arg>`` to, read with the
    arg on the left (``500 >= args.amount`` → ``<=``); Nones otherwise."""
    left, op, right = cmp.left, cmp.op, cmp.right
    if _is_arg(right, arg) and op in _MIRROR:
        left, op, right = right, _MIRROR[op], left
    return (op, right) if _is_arg(left, arg) else (None, None)


def _holds(node: Node, leaf, known: dict) -> bool:
    """Whether ``leaf`` holds on every path through ``node``: one part of an
    And is enough, every branch of an Or is needed. A part that reads only the
    ``known`` tool and role is decided: one no call passes bounds vacuously, one
    every call passes bounds nothing (``tool != "x" or args.amount <= 5``). A Not
    never counts: a comparison on a missing or mistyped value is False, so its
    negation passes it. Nor does a quantifier, which binds elements."""
    from hexgate.security.constraints import And, Call, Cmp, Or

    decided = _decided(node, known)
    if decided is not None:
        return not decided
    if isinstance(node, (Cmp, Call)):
        return leaf(node)
    if isinstance(node, (And, Or)):
        results = (_holds(part, leaf, known) for part in node.parts)
        return all(results) if isinstance(node, Or) else any(results)
    return False


def _decided(node: Node, known: dict) -> bool | None:
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
    named = known.get("named_roles")
    if named is not None and ("role",) in paths:
        tested = _role_literals(node)
        if tested is None or not tested <= named:
            return None
        known = {**known, "role": None}  # holds none of them: tests as no role
    return evaluate_constraint(node, known)


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


def _is_fixed(operand: Operand | None) -> bool:
    """A value the caller of the tool doesn't pick: a literal, a const, or a
    field outside ``args`` (``ctx.*``, ``run.*``)."""
    from hexgate.security.constraints import ConstRef, Lit, Ref

    if isinstance(operand, (Lit, ConstRef)):
        return True
    return isinstance(operand, Ref) and operand.path[:1] != ("args",)
