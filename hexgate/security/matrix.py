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
* **Constraints as written.** The clauses are the resolved policy's own constraint
  strings — joined and parenthesised, never rewritten — so what an auditor reads
  is what the engine runs.

Every rendering choice here resolves the same way: a cell may understate a grant,
never overstate one. Reading a control into the document that the engine does not
enforce is the failure this whole module exists to avoid.

Pure: no I/O, no logging, and nothing platform-side. The report assembler passes
in an already-resolved (post-inheritance, post-link) ``PolicySet`` and formats
what comes back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from hexgate.security.file_scope import FILE_TOOL_ARG_NAMES
from hexgate.security.models import FileScope, FileToolPolicy, PolicyMode, ToolPolicy
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

    def default_cell(self, role: str) -> MatrixCell:
        """``role``'s fallback for an ordinary tool the policy never lists.

        Not for ``agent.*`` keys — see :class:`Matrix`.
        """
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
        (tool, role): _cell_for(get_tool_policy(policies[role], tool), tool)
        for tool in tools
        for role in roles
    }
    # ``default_policy`` is a ``BaseToolPolicy``, so it can never carry a
    # ``file_scope``; the tool-name-dependent rendering below does not apply.
    defaults = {role: _cell_for(policies[role].default_policy, None) for role in roles}
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


def _cell_for(tool_policy: ToolPolicy, tool: str | None) -> MatrixCell:
    """Render one already-resolved tool policy as a cell.

    ``tool`` is the name the policy was resolved for, or ``None`` for a role's
    ``default_policy`` fallback. It is needed because ``file_scope`` is
    evaluated against a path argument chosen by tool name.
    """
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
    return MatrixCell(mode=mode, constraint_text=_constraint_text(tool_policy, tool))


def _file_scope_of(tool_policy: ToolPolicy) -> FileScope | None:
    """The tool policy's ``file_scope`` block, or ``None`` if it has none.

    ``None`` covers both shapes that mean "unscoped": a plain
    :class:`~hexgate.security.models.BaseToolPolicy`, and a
    :class:`~hexgate.security.models.FileToolPolicy` that left ``file_scope`` unset.
    """
    if isinstance(tool_policy, FileToolPolicy):
        return tool_policy.file_scope
    return None


def _constraint_text(tool_policy: ToolPolicy, tool: str | None) -> str | None:
    """Render every condition the grant carries, or ``None`` if unconditional.

    File scope is a condition on the call like any constraint, but it is not an
    expression in the constraint grammar, so it is labelled rather than dressed
    up as one — a cell reading "Allow" with nothing beside it must mean the
    tool is genuinely unrestricted.
    """
    clauses = list(tool_policy.constraints)
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
    rather than rendered as no clause at all. Callers have already ruled out the
    unreachable case, so ``tool`` is always in :data:`FILE_TOOL_ARG_NAMES` here.
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
