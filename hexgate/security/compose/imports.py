"""Resolve the compose import graph — splice imported leaf fragments per scope.

``import:`` is *position = scope*: a fragment imported in a role body applies to
that cell, in an agent body to that agent, at the top level to everything. The
resolver walks the scopes relevant to the agent being resolved, loads each
referenced fragment via a ``loader``, and flattens the transitive closure of leaf
fragments onto that scope's ``_imported``. The linker then unions those fragments
with the local blocks like any other capability — imports *compose*, never
replace, and the fold is commutative so import order never matters.

Paths resolve **relative to the importing file's directory** and are sandboxed to
the project tree (no absolute paths, no ``..`` escape). Only the scopes the target
agent actually resolves are walked, so an unrelated agent's broken import can't
abort resolving a valid one. Files are parsed once per resolution (cached).

Scope for this increment: an imported target must be **leaf-only**
(``tools`` / ``reach`` / ``mcp`` / ``boundary``); importing a file that declares
``agents:`` / ``roles:`` is a later increment, as are globs. A cycle in the import
graph is a :class:`LinkError`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePosixPath

from hexgate.security.compose.grammar import Entry, _GrantScope
from hexgate.security.compose.parse import parse_entry
from hexgate.security.modules import DEFAULT_AGENT, LinkError

# project-relative posix path → the file's text. Injected so imports resolve
# against a real directory for ``resolve_file`` and in-memory for tests.
Loader = Callable[[str], str]


class _Ctx:
    """Per-resolution shared state: the loader, the entry's own path (to detect a
    self-reference), and a parse cache so each file is read/parsed once."""

    def __init__(self, loader: Loader | None, entry_path: str | None) -> None:
        self.loader = loader
        self.entry_path = entry_path
        self.parsed: dict[str, Entry] = {}


def resolve_imports(
    entry: Entry,
    *,
    agent: str,
    loader: Loader | None,
    source: str,
    entry_path: str | None = None,
) -> None:
    """Populate ``_imported`` for the scopes the given ``agent`` resolves.

    Only the top level plus (for a named agent) that agent's body and its roles
    are walked — never other agents — so per-agent resolution is isolated. Files
    are parsed once and their flattened fragment lists memoized across the walk;
    ``entry_path`` (the entry's own project-relative path, when known) lets a
    fragment that imports the entry back be reported as a cycle.
    """
    ctx = _Ctx(loader, entry_path)
    root = PurePosixPath()
    _resolve_scope(entry, source=source, cur_dir=root, ctx=ctx)
    agent_block = entry.agents.get(agent) if agent != DEFAULT_AGENT else None
    if agent_block is not None:
        _resolve_scope(agent_block, source=source, cur_dir=root, ctx=ctx)
        for role in agent_block.roles.values():
            _resolve_scope(role, source=source, cur_dir=root, ctx=ctx)


def _resolve_scope(
    scope: _GrantScope, *, source: str, cur_dir: PurePosixPath, ctx: _Ctx
) -> None:
    scope._source = source  # local grants carry the entry file for provenance
    # `seen` de-dups by RESOLVED key across this scope's whole import closure, so a
    # fragment reached via two refs (a diamond, or `x.yaml` vs `./x.yaml`) is
    # contributed once; it also short-circuits the re-walk of a shared subtree.
    seen: set[str] = set()
    fragments: list[_GrantScope] = []
    for ref in scope.imports:
        fragments.extend(
            _load(ref, source=source, cur_dir=cur_dir, stack=(), ctx=ctx, seen=seen)
        )
    scope._imported = fragments


def _split_ref(ref: str) -> tuple[str, str | None]:
    """``"caps.yaml#refunds"`` → ``("caps.yaml", "refunds")``; no ``#`` → name None."""
    path, _, name = ref.partition("#")
    return path, (name or None)


def _resolve_path(
    cur_dir: PurePosixPath, ref_path: str, *, source: str
) -> PurePosixPath:
    """Join ``ref_path`` onto ``cur_dir`` (the importing file's dir) and normalize,
    rejecting an absolute path or one that escapes the project root via ``..``."""
    candidate = PurePosixPath(ref_path)
    if candidate.is_absolute():
        raise LinkError(f"{source}: absolute import path not allowed: {ref_path!r}")
    parts: list[str] = []
    for part in (cur_dir / candidate).parts:
        if part == "..":
            if not parts:
                raise LinkError(
                    f"{source}: import escapes the project directory: {ref_path!r}"
                )
            parts.pop()
        elif part != ".":
            parts.append(part)
    return PurePosixPath(*parts)


def _load(
    ref: str,
    *,
    source: str,
    cur_dir: PurePosixPath,
    stack: tuple[str, ...],
    ctx: _Ctx,
    seen: set[str],
) -> list[_GrantScope]:
    """Load one import ref → the flat transitive list of leaf fragments it pulls in."""
    if ctx.loader is None:
        raise LinkError(
            f"{source}: 'import:' needs a base path to resolve {ref!r} — resolve "
            f"via a file (resolve_file) or pass a loader"
        )
    path_str, name = _split_ref(ref)
    target = _resolve_path(cur_dir, path_str, source=source)
    target_str = str(target)
    key = f"{target_str}#{name or ''}"
    if key in stack:  # a true cycle: the ref is its own ancestor
        raise LinkError(f"{source}: import cycle: {' -> '.join((*stack, key))}")
    if target_str == ctx.entry_path:
        raise LinkError(
            f"{source}: import cycle: {ref!r} imports the entry file itself"
        )
    if key in seen:  # already contributed in this scope's closure (a diamond edge)
        return []
    seen.add(key)

    imported = ctx.parsed.get(target_str)
    if imported is None:
        try:
            text = ctx.loader(target_str)
        except OSError as exc:
            raise LinkError(f"{source}: cannot import {ref!r}: {exc}") from exc
        imported = parse_entry(text, source=target_str)
        ctx.parsed[target_str] = imported

    if imported.agents:
        raise LinkError(
            f"{target_str}: an imported file must be leaf-only "
            f"(tools/reach/mcp/boundary); it declares agents:/roles:, which "
            f"imports do not support yet"
        )

    if name is not None:
        fragment: _GrantScope | None = imported.exports.get(name)
        if fragment is None:
            known = sorted(imported.exports) or "none"
            raise LinkError(
                f"{target_str}: no export named {name!r} (exports: {known})"
            )
    else:
        # Whole-file import: the file's top-level leaf blocks (its exports are for
        # other importers; an Entry with no agents is itself a leaf scope).
        fragment = imported

    if fragment.boundary is not None:
        raise LinkError(
            f"{target_str}: an imported fragment may only grant (tools/reach/mcp); "
            f"a boundary/ceiling must be authored in the importing file — an "
            f"imported ceiling would intersect and can silently deny every grant"
        )

    fragment._source = target_str
    # Flatten transitively; a fragment resolves ITS imports relative to its own
    # file's directory, cycle-guarded by the ref key and de-duped via ``seen``.
    out: list[_GrantScope] = [fragment]
    child_dir = target.parent
    for sub in fragment.imports:
        out.extend(
            _load(
                sub,
                source=target_str,
                cur_dir=child_dir,
                stack=(*stack, key),
                ctx=ctx,
                seen=seen,
            )
        )
    return out
