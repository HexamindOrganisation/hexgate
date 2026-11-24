"""The checks a set of draft policy files must pass before it may be saved.

:func:`run_checks` runs them in a fixed order, each on the stored files with the
drafts laid over them:

1. ``yaml``: every draft parses as a policy document.
2. ``composes``: the project resolves for every agent.
3. ``lints``: the analyzer's findings. An ``error`` lint the drafts introduce
   (a ``link-error`` among them) is a ``fail``; any other lint is a ``warn``.
4. ``dry_runs``: each sample tool call gets the outcome it expects.
5. ``boundary``: no ``boundary:`` block is loosened.

The first ``fail`` stops the run: every later check is reported ``skipped``, so
a check that did not run can never read as ``pass``. Nothing is written; the
caller saves only when :attr:`ChecksResult.passed` is true.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.features.policy_modules import service
from hexgate_api.features.policy_modules.boundary import (
    WIDENED,
    BoundaryChange,
    compare_boundaries,
)
from hexgate_api.schemas import PolicyFileDraft, PolicyLintOut

if TYPE_CHECKING:  # the SDK is imported lazily at run time
    from hexgate.security import PolicyLint

# ``bundle`` is not run here: the caller reports it, after building the
# bundles from the saved files (see ``apply.py``).
CheckId = Literal["yaml", "composes", "lints", "dry_runs", "boundary", "bundle"]
Status = Literal["pass", "warn", "fail", "skipped"]

# A check lists at most this many items; its detail counts the rest.
MAX_ITEMS = 50


class Check(BaseModel):
    """One check's result. ``items`` are what it found: the files that do not
    parse, the lints, the samples whose outcome differs."""

    id: CheckId
    status: Status
    detail: str
    items: list[dict[str, Any]] = []


class Sample(Protocol):
    """A tool call and the outcome the caller expects for it."""

    agent: str
    role: str
    tool: str
    args: dict
    attributes: dict
    expect: Literal["allow", "deny", "approval_required"]


@dataclass(frozen=True)
class ChecksResult:
    checks: list[Check]
    # Every change the boundary comparison reported, as reported. Empty when
    # the boundary check did not run or raised.
    boundary_changes: list[BoundaryChange]

    @property
    def passed(self) -> bool:
        """Whether the drafts may be saved: every check ran and none failed."""
        return all(c.status in ("pass", "warn") for c in self.checks)


@dataclass
class _Run:
    session: AsyncSession
    project_id: str
    drafts: list[PolicyFileDraft]
    samples: Sequence[Sample]
    # The drafted project's lints, as the composes check computed them.
    lints: list[PolicyLint] = field(default_factory=list)


async def run_checks(
    session: AsyncSession,
    project_id: str,
    files: Sequence[PolicyFileDraft],
    samples: Sequence[Sample],
) -> ChecksResult:
    """Check ``files`` (drafts laid over the project's stored files by name)
    against ``samples``. Every check is reported, in order; none raises for a
    draft the checks refuse."""
    run = _Run(session, project_id, list(files), samples)
    checks: list[Check] = []
    for check_id, check in _CHECKS:
        failed = _first_fail(checks)
        checks.append(
            await check(run) if failed is None else _skipped(check_id, failed)
        )
    failed = _first_fail(checks)
    if failed is None:
        boundary, changes = await _boundary(run)
    else:
        boundary, changes = _skipped("boundary", failed), []
    return ChecksResult(checks=[*checks, boundary], boundary_changes=changes)


def _first_fail(checks: list[Check]) -> CheckId | None:
    return next((c.id for c in checks if c.status == "fail"), None)


def _skipped(check_id: CheckId, failed: CheckId) -> Check:
    return Check(id=check_id, status="skipped", detail=f"{failed} failed")


def _check(
    check_id: CheckId,
    status: Status,
    detail: str,
    items: Sequence[dict[str, Any]] = (),
) -> Check:
    """A check with at most :data:`MAX_ITEMS` items; the detail counts the rest."""
    if len(items) > MAX_ITEMS:
        detail = f"{detail}; {len(items) - MAX_ITEMS} more not listed"
    return Check(
        id=check_id, status=status, detail=detail, items=list(items[:MAX_ITEMS])
    )


async def _yaml(run: _Run) -> Check:
    items = []
    for draft in run.drafts:
        try:
            service.validate_compose_file(draft.content, source=draft.name)
        except service.InvalidModuleError as exc:
            items.append({"file": draft.name, "error": str(exc)})
    if items:
        return _check("yaml", "fail", f"{len(items)} file(s) do not parse", items)
    return _check("yaml", "pass", f"{len(run.drafts)} file(s) parse")


async def _composes(run: _Run) -> Check:
    try:
        resolved, _, lints = await service.compose_preview(
            run.session, run.project_id, drafts=run.drafts
        )
    except RecursionError:  # a constraint nested past the parser's stack
        detail = "a constraint is nested too deeply"
        return _check("composes", "fail", detail)
    if resolved is None:
        items = [{"error": lint.message} for lint in lints]
        return _check("composes", "fail", "the project does not resolve", items)
    run.lints = lints  # the same findings check_auto returns for these files
    return _check("composes", "pass", "the project resolves for every agent")


async def _lints(run: _Run) -> Check:
    """Fails on an error lint whose defect (:func:`_defect`) the saved files
    do not have anywhere. An error the saved files already have is a
    ``warn``, wherever the drafts report it: a project already red must not
    block every later change, and some errors (``unknown-guard``) follow the
    registered agents, which change outside the policy files."""
    lints = run.lints
    saved: set[tuple] = set()
    if any(lint.severity == "error" for lint in lints):
        saved_lints = await service.check_auto(run.session, run.project_id)
        saved = {_defect(lint) for lint in saved_lints}
    new = [lint.severity == "error" and _defect(lint) not in saved for lint in lints]
    items = [_lint_item(lint, is_new) for lint, is_new in zip(lints, new, strict=True)]
    # The new errors first, so the cap never hides what fails the check.
    items.sort(key=lambda item: not item.get("new"))
    if any(new):
        return _check("lints", "fail", f"{sum(new)} new error lint(s)", items)
    if lints:
        return _check("lints", "warn", f"{len(lints)} lint(s)", items)
    return _check("lints", "pass", "no lints")


def _lint_item(lint, new: bool) -> dict[str, Any]:
    """The lint's wire form; an error the drafts introduce, which is what
    fails the check, carries ``new: true``."""
    item = PolicyLintOut.from_lint(lint).model_dump(exclude_none=True)
    return {**item, "new": True} if new else item


# Where the SDK analyzer names the roles carrying a defect: the clause a
# cross-role lint adds when only some roles carry it, and the owner a per-role
# drift lint opens with.
_ROLE_CLAUSE = re.compile(r" in role '[^']*'(?:, role '[^']*')*")
_ROLE_OWNER = re.compile(r"^role '[^']*' ")


def _strip_roles(message: str) -> str:
    """``message`` without the roles that carry its defect. Mirrors the SDK
    analyzer (hexgate/security/analyzer.py): the ``where`` clause,
    ``" in " + ", ".join(f"role {r!r}")``, left out once every role carries
    the defect (lines 1337, 1373, 1460), and the ``owner``, ``f"role {role!r}"``,
    that a resolved role's drift lint opens with (line 1247, used at 901, 919
    and 975). Its tests run the real analyzer, so a reworded clause fails them."""
    message = _ROLE_OWNER.sub("", message, count=1)
    return _ROLE_CLAUSE.sub("", message, count=1)


def _defect(lint) -> tuple:
    """What a lint finds, wherever it is reported. The line, the agent, the
    role and the roles the message names (:func:`_strip_roles`) say where: a
    draft that removes a role, or the defect from the top level while an agent
    keeps it, moves those without adding anything."""
    message = _strip_roles(lint.message)
    return (lint.code, lint.severity, lint.source, lint.tool, message)


async def _dry_runs(run: _Run) -> Check:
    items = []
    for index, sample in enumerate(run.samples):
        outcome, reason = await _dry_run(run, sample)
        if outcome != sample.expect:
            items.append(
                {
                    "index": index,
                    "agent": sample.agent,
                    "role": sample.role,
                    "tool": sample.tool,
                    "expect": sample.expect,
                    "outcome": outcome,
                    "reason": reason,
                }
            )
    total = len(run.samples)
    if items:
        detail = f"{len(items)} of {total} sample(s) differ from expect"
        return _check("dry_runs", "fail", detail, items)
    detail = f"{total} sample(s) as expected" if total else "no samples"
    return _check("dry_runs", "pass", detail)


async def _dry_run(run: _Run, sample: Sample) -> tuple[str | None, str | None]:
    """``(outcome, reason)`` for one sample; the outcome is None when the
    sample names a role or an agent the policy does not define, which is no
    outcome."""
    try:
        dry_run = await service.test_policy(
            run.session,
            run.project_id,
            role=sample.role,
            tool=sample.tool,
            agent=sample.agent,
            args=sample.args,
            attributes=sample.attributes,
            drafts=run.drafts,
        )
    except service.UnknownRoleError as exc:
        return None, str(exc)
    if not dry_run.agent_declared:  # evaluated as "*", not as this agent
        declared = dry_run.declared_agents
        return None, f"agent {sample.agent!r} is not declared (declared: {declared})"
    verdict = dry_run.verdict
    return service.verdict_outcome(verdict), verdict.reason


async def _boundary(run: _Run) -> tuple[Check, list[BoundaryChange]]:
    """The check and every change the comparison reported. Fails on any
    widened block, and when the comparison itself refuses the drafts (a first
    ``policy.yaml``, a drafted block that does not lower): an unchecked
    boundary is never a pass. A saved block outside ``policy.yaml`` that does
    not lower is a ``warn``: it holds no ceiling to compare."""
    from hexgate.security.modules import LinkError

    saved = await service._files_map(run.session, run.project_id)
    drafted = service.overlay_drafts(saved, run.drafts)
    try:
        comparison = compare_boundaries(saved, drafted)
    except (service.InvalidModuleError, LinkError) as exc:
        return _check("boundary", "fail", str(exc)), []
    except RecursionError:  # a constraint nested past the parser's stack
        return _check("boundary", "fail", "a constraint is nested too deeply"), []
    changes = comparison.changes
    widened = sum(change.kind == WIDENED for change in changes)
    if widened:
        detail = f"{widened} boundary change(s) widen a ceiling"
        return _check("boundary", "fail", detail), changes
    detail = f"{len(changes)} boundary change(s), none widen"
    if comparison.uncompared:
        items = [
            {"file": name, "error": "saved block does not lower; no ceiling to compare"}
            for name in comparison.uncompared
        ]
        return _check("boundary", "warn", detail, items), changes
    return _check("boundary", "pass", detail), changes


# Every check but the last, boundary, which also returns its changes.
_CHECKS: tuple[tuple[CheckId, Callable[[_Run], Awaitable[Check]]], ...] = (
    ("yaml", _yaml),
    ("composes", _composes),
    ("lints", _lints),
    ("dry_runs", _dry_runs),
)
