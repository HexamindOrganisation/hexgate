"""Resolve a ``policy.yaml`` into a role-keyed :class:`ProjectLinkResult`.

The orchestration seam: parse the entry file, lower it per ``(agent, role)``, and
fold each cell through the existing :func:`~hexgate.security.linker.link_policy_set`
— assembling the same ``ProjectLinkResult`` shape :func:`resolve_for_project`
produces, so ``effective_policy_by_role`` and the rego/wasm/signing path consume
it unchanged. Folding per cell (rather than calling ``resolve_for_project``) is
what lets a per-agent / per-role ``boundary`` apply only to its own cell.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from hexgate.security.compose.grammar import Entry
from hexgate.security.compose.lower import lower
from hexgate.security.compose.parse import parse_entry
from hexgate.security.constraints import ConstraintParseError
from hexgate.security.linker import link_policy_set
from hexgate.security.models import AgentPolicy
from hexgate.security.modules import (
    DEFAULT_AGENT,
    LinkError,
    LinkResult,
    ProjectLinkResult,
)
from hexgate.security.policy_set import DEFAULT_ROLE_NAME, PolicySet, PolicySetError


def resolve_entry(
    entry: Entry, *, agent: str = DEFAULT_AGENT, source: str = "policy.yaml"
) -> ProjectLinkResult:
    """Resolve an already-parsed :class:`Entry` for one executing ``agent``.

    Split from :func:`resolve_text` so a caller that already parsed (the CLI, to
    read the declared agents for a hint) resolves without parsing twice.
    """
    if entry.imports or entry.exports:
        raise LinkError(
            f"{source}: 'import:'/'export:' are not supported yet — the import "
            f"graph lands in the next increment; inline the fragments for now"
        )

    # lower()/link build the SDK models, whose own validators (a bad constraint,
    # a reserved agent.* tool key, an empty `as:`) raise pydantic/constraint
    # errors. Surface them as a source-named LinkError, honouring the contract
    # that parse/resolve failures come back as LinkError with the file named.
    try:
        per_role = lower(entry, agent)
        by_role: dict[str, LinkResult] = {}
        effective: dict[str, AgentPolicy] = {}
        for role, (boundaries, caps) in per_role.items():
            result = link_policy_set(boundaries, caps)
            by_role[role] = result
            effective[role] = result.effective[DEFAULT_ROLE_NAME]
        return ProjectLinkResult(policy_set=PolicySet(effective), by_role=by_role)
    except LinkError:
        raise  # already carries module provenance
    except (ValidationError, ConstraintParseError, PolicySetError) as exc:
        raise LinkError(f"{source}: {exc}") from exc


def resolve_text(
    text: str, *, agent: str = DEFAULT_AGENT, source: str = "policy.yaml"
) -> ProjectLinkResult:
    """Resolve one policy document's text for one executing ``agent``."""
    return resolve_entry(parse_entry(text, source=source), agent=agent, source=source)


def resolve_file(path: str | Path, *, agent: str = DEFAULT_AGENT) -> ProjectLinkResult:
    """Resolve the ``policy.yaml`` at ``path`` for one executing ``agent``."""
    p = Path(path)
    return resolve_text(p.read_text(encoding="utf-8"), agent=agent, source=str(p))
