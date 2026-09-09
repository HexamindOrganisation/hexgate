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
from hexgate.security.compose.imports import Loader, resolve_imports
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


def file_loader(base_dir: str | Path) -> Loader:
    """A filesystem loader rooted at ``base_dir``, sandboxed to that tree.

    The resolver's lexical guard already rejects ``..``/absolute refs; this also
    resolves real paths (following symlinks) and rejects a target that escapes
    ``base_dir`` — e.g. a symlink inside the project pointing outside it.
    """
    base = Path(base_dir).resolve()

    def load(rel_path: str) -> str:
        target = (base / rel_path).resolve()
        if not target.is_relative_to(base):
            raise LinkError(
                f"import escapes the project directory: {rel_path!r} → {target}"
            )
        return target.read_text(encoding="utf-8")

    return load


def resolve_entry(
    entry: Entry,
    *,
    agent: str = DEFAULT_AGENT,
    source: str = "policy.yaml",
    loader: Loader | None = None,
    entry_path: str | None = None,
) -> ProjectLinkResult:
    """Resolve an already-parsed :class:`Entry` for one executing ``agent``.

    Split from :func:`resolve_text` so a caller that already parsed (the CLI, to
    read the declared agents for a hint) resolves without parsing twice. ``loader``
    resolves ``import:`` refs; a policy that imports without one errors.
    ``entry_path`` is the entry's own project-relative path, so a fragment that
    imports the entry back is caught as a cycle.
    """
    # Splice imported leaf fragments onto each scope's ``_imported`` first — only
    # the scopes this agent resolves, so an unrelated agent's bad import can't
    # abort us. Raises a source-named LinkError on a bad ref, cycle, or missing loader.
    resolve_imports(
        entry, agent=agent, loader=loader, source=source, entry_path=entry_path
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
    text: str,
    *,
    agent: str = DEFAULT_AGENT,
    source: str = "policy.yaml",
    loader: Loader | None = None,
    entry_path: str | None = None,
) -> ProjectLinkResult:
    """Resolve one policy document's text for one executing ``agent``.

    ``loader`` (import-ref path → text) resolves ``import:``; omit it for a
    self-contained policy, or pass one to resolve imports in memory (tests).
    """
    return resolve_entry(
        parse_entry(text, source=source),
        agent=agent,
        source=source,
        loader=loader,
        entry_path=entry_path,
    )


def resolve_file(path: str | Path, *, agent: str = DEFAULT_AGENT) -> ProjectLinkResult:
    """Resolve the ``policy.yaml`` at ``path`` for one executing ``agent``.

    ``import:`` refs resolve relative to the entry file's directory, sandboxed to
    that tree.
    """
    p = Path(path)
    return resolve_text(
        p.read_text(encoding="utf-8"),
        agent=agent,
        source=str(p),
        loader=file_loader(p.parent),
        entry_path=p.name,
    )
