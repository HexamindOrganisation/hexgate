"""Tabulate a resolved :class:`PolicySet` as a roles x tools authorisation matrix.

The lint layer (:mod:`hexgate.security.analyzer`) reports *problems*; the engines
(:mod:`hexgate.security.policy`, :mod:`hexgate.security.wasm_engine`) answer *one*
proposed call. Neither renders the standing grant table an auditor asks for: which
role may reach which tool, and under what constraint. That is what this module
produces.

Three properties it must keep, because a compliance document is downstream of it:

* **Per role, not per caller.** :func:`~hexgate.security.decision.combine_role_verdicts`
  takes the permissive union across the roles one caller happens to carry. The
  matrix does not: each column is that role's own standing grant, which is the
  thing being evidenced.
* **The engine's own fallback.** A cell is resolved through
  :func:`~hexgate.security.policy.get_tool_policy`, so the table says what the
  engine would actually decide. Absence means ``deny`` for any policy that keeps
  the default ``default_policy`` (deny-by-default, the standing posture) — but a
  role that deliberately sets a permissive ``default_policy`` really does allow
  the tools it never lists, and :attr:`Matrix.defaults` carries that rather than
  letting the grid assert a control nobody enforces.
* **Constraint expressions carried verbatim.** Each expression is the resolved
  policy's own string, never reworded. The *cell* composes them — the role-wide
  ``AgentPolicy.constraints`` fence first, then the tool's own, in the order
  :func:`~hexgate.security.policy.evaluate_tool_call` checks them, each
  parenthesised once anything is joined to it — and, for a path-scoped tool
  only, appends a synthesised ``file_scope:`` clause, which is a label rather
  than policy text. Without that clause the composed string is still inside the
  constraint grammar; with it the cell is a faithful reading of the conditions
  rather than an expression the engine would parse. ``consts.<name>`` references
  render as written, so a cell states the condition without resolving the value.

Every rendering choice here resolves the same way: a cell may understate a grant,
never overstate one. Reading a control into the document that the engine does not
enforce is the failure this whole module exists to avoid.

Pure: no I/O, no logging, and nothing platform-side. The report assembler passes
in an already-resolved (post-inheritance, post-link) ``PolicySet`` and formats
what comes back.

Two decisions worth recording, so they are not re-litigated:

*Why this lives in the SDK.* It sits next to :class:`PolicySet` and
:mod:`hexgate.security.decision`, the code that actually decides a call, so the
table cannot drift from the evaluator it claims to evidence — a change to the
fallback rules breaks this module's tests in the same commit. The placement also
keeps one answer reachable from both the CLI and the platform rather than each
growing its own idea of what a policy grants; no CLI command renders a matrix
today, but here it costs an import rather than a second implementation.

*On the dashboard's TypeScript resolver.*
``platform/dashboard/src/lib/policy.ts`` carries its own ``inherits``/mixin
resolver over the raw ``policy_yaml``. It is **not** what the Graph tab draws —
that route renders what the server resolved, via
``GET /v1/projects/{id}/policy/graph`` (``routes/Graph.tsx`` ->
``components/policy_files/PolicyGraphDialog`` -> ``lib/api.ts``). Outside its own
tests the module's single live export is ``parseRolesFromPolicy``, which lists
concrete role names for the Playground role picker and resolves no inheritance,
so the resolver itself is effectively dead. It could not serve the report in any
case — it never sees the compiled bundle. Its edge-case handling is still worth
agreeing with where it is right, so ``tests/security/test_matrix.py`` pins the
cases and names which side wins where the two differ.

*The live second answer is server-side, and it differs.*
``platform/api/.../features/policy_modules/service.py`` (``_graph_from``, behind
``GET /v1/projects/{id}/policy/graph``) walks the same linked ``effective_tools``
per role and is what the Graph route actually draws. It reads only each tool's
own ``constraints``, so a role-wide ``AgentPolicy.constraints`` fence does not
reach its edges and a grant renders less conditional there than the engine
enforces; it also renders no ``file_scope``. This module is the correct side on
both counts. Worth reconciling when the graph and the report are asked to agree
— not in this PR, which adds no endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hexgate.security.file_scope import FILE_TOOL_ARG_NAMES
from hexgate.security.models import (
    AgentPolicy,
    FileScope,
    FileToolPolicy,
    PolicyMode,
    ToolPolicy,
    is_agent_key,
    is_skill_key,
)
from hexgate.security.policy import get_tool_policy
from hexgate.security.policy_set import DEFAULT_ROLE_NAME, PolicySet

#: A cell's authorisation mode. Narrower than
#: :data:`~hexgate.security.models.PolicyMode`: ``approval_required`` is the
#: document-schema spelling, ``approval`` the column heading an auditor reads.
MatrixMode = Literal["allow", "approval", "deny"]

_MODE_BY_POLICY_MODE: dict[PolicyMode, MatrixMode] = {
    "allow": "allow",
    "deny": "deny",
    "approval_required": "approval",
}


@dataclass(frozen=True, slots=True)
class MatrixCell:
    """One role's standing authorisation for one tool.

    ``constraint_text`` is ``None`` when the grant is unconditional, and is
    always ``None`` on a deny — the engine short-circuits a deny before it reads
    a constraint, so printing one would suggest the tool becomes reachable when
    the constraint holds.
    """

    mode: MatrixMode
    constraint_text: str | None = None


_DENIED = MatrixCell(mode="deny")


@dataclass(frozen=True, slots=True)
class Matrix:
    """The full roles x tools grid for one agent's resolved policy.

    Unrelated to :data:`~hexgate.security.modules.RoleMatrix`, also exported from
    this package, which maps a role to the capability modules it imports.

    ``roles`` leads with ``default`` and continues alphabetically; ``tools`` is
    the sorted union of every role's effective tools, including the lowered
    ``agent.*`` keys, so an admission or reach rule is evidenced alongside
    ordinary tools. ``cells`` is complete: every ``(tool, role)`` pair has an
    entry, keyed in that order.

    ``defaults`` holds each role's fallback for *ordinary* tools outside
    ``tools`` — deny for a deny-by-default policy, which is the whole grid's
    premise, and the one place a permissive ``default_policy`` becomes visible.
    It deliberately does not speak for unlisted ``agent.*`` keys: those are
    closed-world and always deny, whatever ``default_policy`` says.

    ``aliased_default`` names the role the loader promoted into the ``default``
    slot when the document declared none (:attr:`PolicySet.aliased_default`).
    When it is set, the ``default`` column is a duplicate of that role rather
    than a baseline anyone authored, and a report that presents it as one would
    be evidencing a role the policy never declared.
    """

    roles: tuple[str, ...]
    tools: tuple[str, ...]
    cells: dict[tuple[str, str], MatrixCell]
    defaults: dict[str, MatrixCell]
    aliased_default: str | None = None

    def cell(self, tool: str, role: str) -> MatrixCell:
        """The cell at ``(tool, role)``. Raises :class:`KeyError` off the grid.

        Off-grid is a caller bug, not a deny: answering ``deny`` for a
        misspelled tool name would quietly evidence a control that was never
        looked up.
        """
        return self.cells[(tool, role)]

    def default_cell(self, tool: str, role: str) -> MatrixCell:
        """What ``role`` gets for ``tool`` when the grid does not list it.

        Argument order matches :meth:`cell` so the two cannot be transposed by
        habit. ``tool`` is required because the answer depends on it: an ``agent.*``
        or ``skill*`` key is closed-world and denies whatever ``default_policy``
        says, so a role with a permissive default must not be reported as reaching
        an agent or a skill its policy never named. Reading :attr:`defaults` directly answers the
        narrower question — the fallback for an *ordinary* tool — and is what a
        report's "any other tool" row should state.
        """
        if is_agent_key(tool) or is_skill_key(tool):
            return _DENIED
        return self.defaults[role]


def authorisation_matrix(policy_set: PolicySet) -> Matrix:
    """Tabulate ``policy_set`` as roles x tools -> :class:`MatrixCell`.

    ``policy_set`` is expected to be resolved already — inheritance flattened
    and modules folded — because that is the policy the engines run. Passing an
    unresolved set tabulates exactly what was passed; this function composes
    nothing.
    """
    roles = _ordered_roles(policy_set)
    policies = {role: policy_set.policy_for(role) for role in roles}
    tools = tuple(
        sorted({tool for pol in policies.values() for tool in pol.effective_tools})
    )
    cells = {
        (tool, role): _cell_for(policies[role], tool)
        for tool in tools
        for role in roles
    }
    defaults = {role: _cell_for(policies[role], None) for role in roles}
    return Matrix(
        roles=roles,
        tools=tools,
        cells=cells,
        defaults=defaults,
        aliased_default=policy_set.aliased_default,
    )


def _ordered_roles(policy_set: PolicySet) -> tuple[str, ...]:
    """``default`` first, then the named roles alphabetically.

    ``default`` leads because it is the fallback the others are read against:
    an auditor comparing a named role's column to the baseline wants the
    baseline on the left. The alphabetical tail is inherited, not imposed —
    :attr:`PolicySet.roles` is already sorted, and it always contains
    ``default``, so this can neither drop nor duplicate a role.
    """
    named = [role for role in policy_set.roles if role != DEFAULT_ROLE_NAME]
    return (DEFAULT_ROLE_NAME, *named)


def _cell_for(policy: AgentPolicy, tool: str | None) -> MatrixCell:
    """Render one role's authorisation for one tool as a cell.

    ``tool`` is ``None`` for the role's ``default_policy`` fallback — the cell
    for every ordinary tool the policy never lists. Otherwise the tool policy is
    resolved through :func:`get_tool_policy`, the single definition of what
    absence means: ``default_policy`` for an ordinary tool, closed-world deny
    for an unlisted ``agent.*`` key. Reimplementing that here is how the table
    would come to disagree with the engine it is evidencing.

    The tool name also decides the ``file_scope`` rendering, which is evaluated
    against a path argument chosen by tool name. A ``default_policy`` loaded from
    a document can never carry a scope — the field is typed ``BaseToolPolicy``
    and ``extra="forbid"`` rejects the key — and one constructed in Python with a
    ``FileToolPolicy`` still renders safely, since ``None`` is not in
    ``FILE_TOOL_ARG_NAMES`` and the unreachable-scope branch below denies it.
    """
    tool_policy = (
        policy.default_policy if tool is None else get_tool_policy(policy, tool)
    )
    mode = _MODE_BY_POLICY_MODE[tool_policy.mode]
    if mode == "deny":
        return _DENIED
    if _file_scope_of(tool_policy) is not None and tool not in FILE_TOOL_ARG_NAMES:
        # ``is_path_allowed`` reads the scoped path out of one argument chosen by
        # tool name; for a tool it has no entry for, ``extract_scoped_path``
        # returns None and every call denies. The grant is unreachable, so the
        # honest cell is a deny — rendering it as an allow under a condition
        # nothing can satisfy would evidence a control that is really a block.
        return _DENIED
    return MatrixCell(
        mode=mode, constraint_text=_constraint_text(policy, tool_policy, tool)
    )


def _file_scope_of(tool_policy: ToolPolicy) -> FileScope | None:
    """The tool policy's ``file_scope`` block, or ``None`` if it has none.

    ``None`` covers both shapes that mean "unscoped": a plain
    :class:`~hexgate.security.models.BaseToolPolicy`, and a
    :class:`~hexgate.security.models.FileToolPolicy` that left ``file_scope`` unset.
    """
    if isinstance(tool_policy, FileToolPolicy):
        return tool_policy.file_scope
    return None


def _constraint_text(
    policy: AgentPolicy, tool_policy: ToolPolicy, tool: str | None
) -> str | None:
    """Render every condition the grant carries, or ``None`` if unconditional.

    The role-wide fence leads, then the tool's own — the order
    :func:`~hexgate.security.policy.evaluate_tool_call` concatenates them in.
    Order is cosmetic to the engine (all of them must pass), but a reader should
    meet the clauses in the order the engine reports a denial from.

    File scope is a condition on the call like any constraint, but it is not an
    expression in the constraint grammar, so it is labelled rather than dressed
    up as one — a cell reading "Allow" with nothing beside it must mean the
    tool is genuinely unrestricted.
    """
    clauses = [*policy.constraints, *tool_policy.constraints]
    scope = _file_scope_text(tool_policy, tool)
    # Parenthesise the expressions as soon as anything is joined to them, so an
    # ``or`` inside one cannot re-associate across the join. The file-scope
    # clause is labelled prose with no operators, so it is left as written.
    if len(clauses) + bool(scope) > 1:
        clauses = [f"({clause})" for clause in clauses]
    if scope:
        clauses.append(scope)
    return " and ".join(clauses) or None


def _file_scope_text(tool_policy: ToolPolicy, tool: str | None) -> str:
    """Render a ``file_scope`` block as a labelled clause naming its argument.

    A present-but-empty block is still a restriction: ``is_path_allowed`` denies
    a call whose path argument is missing or blank, so the requirement is named
    rather than rendered as no clause at all. Returns ``""`` for the unscoped
    cases this is called on — most tools, and the ``tool is None`` fallback —
    and by the time a scope reaches the lookup below, :func:`_cell_for` has
    already denied the tools that have no entry in
    :data:`FILE_TOOL_ARG_NAMES`.
    """
    scope = _file_scope_of(tool_policy)
    if scope is None or tool is None:
        return ""
    parts = [f"{FILE_TOOL_ARG_NAMES[tool]} must be present"]
    if scope.allowed_paths:
        parts.append(f"within {list(scope.allowed_paths)}")
    if scope.denied_paths:
        parts.append(f"outside {list(scope.denied_paths)}")
    return f"file_scope: {', '.join(parts)}"
