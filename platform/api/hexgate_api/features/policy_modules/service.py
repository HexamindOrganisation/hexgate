"""Policy-module store persistence + resolve/check over the hexgate SDK.

A project's policy is composed from boundary + capability modules and a role
binding, not one policy_yaml per agent (see docs/adr/R-POL-001). This module is
the store (CRUD) plus thin wrappers that turn the stored rows into the SDK's
``ModuleContent`` list and call ``resolve_for_project`` / ``check_project``. The
fold itself lives in the SDK; nothing here reimplements it.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable

import yaml
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.core.ids import new_id
from hexgate_api.models import PolicyFile, PolicyModule, RoleBinding, utcnow

logger = logging.getLogger("hexgate.platform.policy_modules")

VALID_TIERS = ("boundary", "capability")

# The generic/default agent key in a role binding. Mirrors
# ``hexgate.security.DEFAULT_AGENT``; kept local so the store CRUD (get/set_roles)
# doesn't import the SDK at module load — the SDK is imported lazily in resolve.
DEFAULT_AGENT = "*"


class InvalidModuleError(Exception):
    """A module's tier is unknown, or its content doesn't parse as a policy.

    Routes translate this to HTTP 422. Raised before the row is written so a
    malformed module never lands in the store.
    """


class UnknownRoleError(Exception):
    """The role a decision-test targets isn't in the resolved policy set.

    Its own type (not a bare ``KeyError``) so the router maps *this* to HTTP 404
    without also catching a ``KeyError`` the SDK decision engine might raise for an
    unrelated missing key — which should surface as a 500, not a phantom 404.
    """


def _content_hash(content: str) -> str:
    """sha256 of the module's canonical JSON — the SAME scheme the SDK loader
    uses (``hexgate.security.module_loader``), so a module authored on the
    platform and the same module loaded from a file hash identically regardless
    of YAML formatting. ``default=str`` matches the loader for scalars YAML can
    produce that JSON can't (e.g. an unquoted date)."""
    payload = yaml.safe_load(content) or {}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_policy(content: str):
    """Parse a module's YAML into an AgentPolicy. The one place content is parsed,
    shared by write-time validation and read-time ModuleContent building so they
    can't drift."""
    from hexgate.security import AgentPolicy

    return AgentPolicy.model_validate(yaml.safe_load(content) or {})


def _validate_policy_yaml(content: str) -> None:
    """Reject content that isn't a valid AgentPolicy before it's stored."""
    try:
        _parse_policy(content)
    except Exception as exc:  # noqa: BLE001 — surface as a clean 422
        raise InvalidModuleError(f"module is not a valid policy: {exc}") from exc


# --- module CRUD -------------------------------------------------------------


async def list_modules(session: AsyncSession, project_id: str) -> list[PolicyModule]:
    stmt = (
        select(PolicyModule)
        .where(PolicyModule.project_id == project_id)
        .order_by(PolicyModule.tier, PolicyModule.path)  # type: ignore[arg-type]
    )
    return list((await session.exec(stmt)).all())


async def _get_module(
    session: AsyncSession, project_id: str, tier: str, path: str
) -> PolicyModule | None:
    return (
        await session.exec(
            select(PolicyModule).where(
                PolicyModule.project_id == project_id,
                PolicyModule.tier == tier,
                PolicyModule.path == path,
            )
        )
    ).first()


async def get_module_peek(
    session: AsyncSession, project_id: str, tier: str, path: str
) -> tuple[str | None, str | None]:
    """``(content, content_hash)`` of the stored module, or ``(None, None)``.

    One read, so a caller can both detect a real change (compare the hash) and
    restore the prior content if an edit turns out to break the project."""
    row = await _get_module(session, project_id, tier, path)
    return (row.content, row.content_hash) if row is not None else (None, None)


async def upsert_module(
    session: AsyncSession,
    *,
    project_id: str,
    tier: str,
    path: str,
    content: str,
) -> PolicyModule:
    """Create or replace one module. Validates the tier and the policy content.

    Insert falls back to update on the unique constraint, so two concurrent
    creates of the same module don't 500: the loser rolls back and updates the
    row the winner just wrote.
    """
    if tier not in VALID_TIERS:
        raise InvalidModuleError(
            f"unknown tier {tier!r} (expected one of {VALID_TIERS})"
        )
    _validate_policy_yaml(content)

    existing = await _get_module(session, project_id, tier, path)
    if existing is None:
        row = PolicyModule(
            id=new_id(PolicyModule),
            project_id=project_id,
            tier=tier,
            path=path,
            content=content,
            content_hash=_content_hash(content),
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            # A create-race leaves the row present on re-query, so fall through
            # to update it. Any other IntegrityError (e.g. a bad project_id FK)
            # is not a race — re-raise rather than assert on a None re-query.
            existing = await _get_module(session, project_id, tier, path)
            if existing is None:
                raise
        else:
            await session.refresh(row)
            return row

    existing.content = content
    existing.content_hash = _content_hash(content)
    existing.updated_at = utcnow()
    session.add(existing)
    await session.commit()
    await session.refresh(existing)
    return existing


async def delete_module(
    session: AsyncSession, *, project_id: str, tier: str, path: str
) -> bool:
    """Remove one module. Returns False if it didn't exist."""
    row = await _get_module(session, project_id, tier, path)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True


# --- role bindings -----------------------------------------------------------


RoleMatrixJson = dict[str, dict[str, list[str]]]
"""The platform's JSON-friendly binding shape: role -> agent-or-"*" -> caps."""


def _normalize_cell(stored: object) -> dict[str, list[str]]:
    """One stored ``RoleBinding.capabilities`` value → ``{agent: [caps]}``.

    A legacy flat ``[names]`` list reads as the generic ``{"*": [names]}`` agent,
    so old rows keep their exact meaning with no migration. A mapping is already
    the matrix and passes through.
    """
    if isinstance(stored, dict):
        return {str(agent): list(caps) for agent, caps in stored.items()}
    if isinstance(stored, list):
        return {DEFAULT_AGENT: [str(c) for c in stored]}
    # The column is model-constrained to list|dict, so this only fires on a
    # corrupt row (a direct DB edit, a future bug). Fail closed (the role grants
    # nothing) but don't swallow it silently — log so check()/ops can see it.
    logger.warning(
        "role_binding.capabilities has unexpected shape %r; treating as empty",
        type(stored).__name__,
    )
    return {}


async def get_roles(session: AsyncSession, project_id: str) -> RoleMatrixJson:
    """The project's role bindings as ``role -> agent-or-"*" -> capabilities``.

    Legacy flat rows normalize to the generic ``"*"`` agent, so a project written
    before the agent axis reads back identically.
    """
    rows = (
        await session.exec(
            select(RoleBinding).where(RoleBinding.project_id == project_id)
        )
    ).all()
    return {row.role: _normalize_cell(row.capabilities) for row in rows}


async def roles_importing(
    session: AsyncSession, project_id: str, path: str
) -> list[str]:
    """Role names whose binding still imports the capability ``path`` under ANY agent.

    Used to block deleting a capability that a role still references: without
    this the delete succeeds but the project stops resolving (the SDK linker
    raises "role imports unknown capability"), so no new bundle is built and
    every agent keeps the old one — still granting the deleted capability.
    """
    roles = await get_roles(session, project_id)
    return sorted(
        role
        for role, cells in roles.items()
        if any(path in caps for caps in cells.values())
    )


async def set_roles(
    session: AsyncSession,
    *,
    project_id: str,
    roles: RoleMatrixJson | dict[str, list[str]],
) -> RoleMatrixJson:
    """Replace the project's role bindings wholesale (a small, edited-together set).

    Accepts either the matrix (``role -> {agent: [caps]}``) or the flat form
    (``role -> [caps]``, normalized to the generic ``"*"`` agent), so a flat
    caller stays valid. Stores each role's ``{agent: [caps]}`` mapping in the
    row's JSON value — no schema change.

    Retries once on an IntegrityError: two concurrent wholesale replaces can each
    delete the existing rows and re-insert the same ``(project_id, role)``,
    colliding on the unique constraint. The retry re-reads the winner's rows and
    replaces them cleanly instead of surfacing a 500 (same posture as
    ``upsert_module``'s create-race handling).
    """
    normalized = {role: _normalize_cell(cells) for role, cells in roles.items()}
    for attempt in range(2):
        existing = (
            await session.exec(
                select(RoleBinding).where(RoleBinding.project_id == project_id)
            )
        ).all()
        for row in existing:
            await session.delete(row)
        # Emit the DELETEs before the INSERTs — otherwise the unit-of-work can
        # order an INSERT first and trip the (project_id, role) unique constraint
        # when a role name recurs across edits (the common wholesale-replace case).
        await session.flush()
        for role, cells in normalized.items():
            session.add(
                RoleBinding(
                    id=new_id(RoleBinding),
                    project_id=project_id,
                    role=role,
                    capabilities=dict(cells),
                )
            )
        try:
            await session.commit()
            break
        except IntegrityError:
            await session.rollback()
            if attempt == 1:
                raise
    return await get_roles(session, project_id)


# --- resolve / check (over the SDK) ------------------------------------------


def _to_module_content(row: PolicyModule):
    from hexgate.security import ModuleContent

    return ModuleContent(
        name=row.path,
        kind=row.tier,  # "boundary" | "capability" == LayerKind
        policy=_parse_policy(row.content),
        source=f"{row.tier}/{row.path}",
        content_hash=row.content_hash,
    )


def _to_role_matrix(matrix: RoleMatrixJson):
    """The stored JSON matrix (``role -> agent -> [caps]``) → the SDK's RoleMatrix
    (AgentBinding cells), or ``None`` when empty.

    No role bindings maps to ``None``, not ``{}``: the SDK reads ``None`` as "no
    roles, one default importing every capability" (the all-compose behaviour the
    local ``hexgate policy resolve`` and docs/adr/R-POL-001 document), whereas
    ``{}`` is a present-but-empty binding that fail-closes. The platform has no
    typo-able roles file, so "no bindings" is the no-roles case, not the empty one.
    """
    from hexgate.security import AgentBinding

    return {
        role: {
            agent: AgentBinding(capabilities=tuple(caps))
            for agent, caps in cells.items()
        }
        for role, cells in matrix.items()
    } or None


async def _sdk_inputs(session: AsyncSession, project_id: str):
    rows = await list_modules(session, project_id)
    boundaries = [_to_module_content(r) for r in rows if r.tier == "boundary"]
    capabilities = [_to_module_content(r) for r in rows if r.tier == "capability"]
    roles = _to_role_matrix(await get_roles(session, project_id))
    return boundaries, capabilities, roles


async def resolve(session: AsyncSession, project_id: str, agent: str = DEFAULT_AGENT):
    """Compose one agent's role-keyed PolicySet (Path A). Raises the SDK's
    LinkError / PolicySetError / ConstraintParseError on an invalid set.

    ``agent`` selects the executing agent's column of the ``(role, agent)`` matrix
    (default ``"*"`` — the generic view). Each agent resolves to its own bundle."""
    from hexgate.security import resolve_for_project

    boundaries, capabilities, roles = await _sdk_inputs(session, project_id)
    return resolve_for_project(boundaries, capabilities, roles, agent=agent)


async def check(session: AsyncSession, project_id: str):
    """Lint the composed project. A hard link failure folds into a single
    error lint inside the SDK, so this always returns a list."""
    from hexgate.security import check_project

    boundaries, capabilities, roles = await _sdk_inputs(session, project_id)
    return check_project(boundaries, capabilities, roles)


def compose_error_types() -> tuple[type[BaseException], ...]:
    """Exceptions meaning "the module set doesn't compose right now".

    The single source of truth shared by the write-time guard (:func:`resolves`)
    and the compile fail-safe (``agents.service``), so they can't drift on which
    errors fold to "keep the last-good bundle" versus escape as a 500 — the SDK
    link/compose errors plus ``yaml.YAMLError`` / pydantic ``ValidationError``
    from re-parsing a stored row. SDK imports stay lazy so this module loads
    without the SDK."""
    from pydantic import ValidationError

    from hexgate.security import LinkError, PolicySetError
    from hexgate.security.constraints import ConstraintParseError

    return (
        LinkError,
        PolicySetError,
        ConstraintParseError,
        ValidationError,
        yaml.YAMLError,
    )


def _all_agent_names(roles) -> set[str]:
    """Every agent column named in the role matrix, plus the generic ``"*"``."""
    agents = {DEFAULT_AGENT}
    if roles:
        agents |= {agent for cells in roles.values() for agent in cells}
    return agents


def _resolve_all_agents(boundaries, capabilities, roles) -> None:
    """Resolve EVERY agent column of ``roles`` (not just ``"*"``, so a named-agent
    cell importing an unknown capability is caught). Raises the SDK compose errors
    on failure; resolves only (no YAML serialization)."""
    from hexgate.security import resolve_for_project

    for agent in _all_agent_names(roles):
        resolve_for_project(boundaries, capabilities, roles, agent=agent)


def normalize_roles(
    roles: RoleMatrixJson | dict[str, list[str]],
) -> RoleMatrixJson:
    """A flat-or-matrix role binding → the matrix JSON shape (flat → ``"*"``)."""
    return {role: _normalize_cell(cells) for role, cells in roles.items()}


async def resolves(session: AsyncSession, project_id: str) -> bool:
    """Whether the project's STORED modules + bindings compose for every agent.

    A cheap, opa-free precondition for accepting a write that edits a MODULE
    (the bindings are already stored). ``True`` only if every agent column
    resolves. ``_sdk_inputs`` is inside the ``try`` because it re-parses every
    stored module — a row that no longer parses is a compose failure (→ False →
    409), not a 500."""
    try:
        boundaries, capabilities, roles = await _sdk_inputs(session, project_id)
        _resolve_all_agents(boundaries, capabilities, roles)
        return True
    except compose_error_types():
        return False


async def resolves_proposed(
    session: AsyncSession, project_id: str, proposed: RoleMatrixJson
) -> bool:
    """Whether the STORED modules compose with the PROPOSED role bindings, for
    every agent — validated in memory **before** writing, so an invalid binding
    is never briefly visible via a concurrent GET and no rollback is needed.
    ``_sdk_inputs`` is inside the ``try`` for the same reason as :func:`resolves`."""
    try:
        boundaries, capabilities, _stored = await _sdk_inputs(session, project_id)
        _resolve_all_agents(boundaries, capabilities, _to_role_matrix(proposed))
        return True
    except compose_error_types():
        return False


def _draft_module_content(tier: str, path: str, content: str):
    """A :class:`ModuleContent` from an unsaved draft (hash recomputed, not stored)."""
    from hexgate.security import ModuleContent

    return ModuleContent(
        name=path,
        kind=tier,
        policy=_parse_policy(content),  # raises on invalid YAML / schema
        source=f"{tier}/{path}",
        content_hash=_content_hash(content),
    )


async def resolves_with_module(
    session: AsyncSession, project_id: str, tier: str, path: str, content: str
) -> bool:
    """Whether the project still resolves with this DRAFT module overlaid, without
    writing it — validated in memory **before** the write, so a resolution-breaking
    edit (e.g. a capability with a deny) is rejected without a commit-then-rollback
    window a concurrent GET could observe.

    Raises :class:`InvalidModuleError` if the content isn't a valid policy (the
    caller maps that to 422); returns ``False`` only when the content is valid but
    the project no longer composes with it (→ 409)."""
    _validate_policy_yaml(content)  # → InvalidModuleError (422) on malformed content
    try:
        boundaries, capabilities, roles = await _sdk_inputs(session, project_id)
        overlay = _draft_module_content(tier, path, content)
        if tier == "boundary":
            boundaries = [m for m in boundaries if m.name != path] + [overlay]
        else:
            capabilities = [m for m in capabilities if m.name != path] + [overlay]
        _resolve_all_agents(boundaries, capabilities, roles)
        return True
    except compose_error_types():
        return False


# --- enforcement integration (see docs/adr/R-POL-002) ------------------------


async def is_modular(session: AsyncSession, project_id: str) -> bool:
    """Whether the project compiles agents from a policy store rather than each
    agent's ``policy_yaml``.

    Modular once the project has either a compose **entry file** (``policy.yaml``)
    or at least one **role binding** (the tier store). Either is the deliberate
    opt-in: a half-built library (a lone capability, or non-entry files) does not
    flip enforcement, so live agents are never bricked mid-authoring.
    """
    if await has_entry_file(session, project_id):
        return True
    row = (
        await session.exec(
            select(RoleBinding.id)  # type: ignore[arg-type]
            .where(RoleBinding.project_id == project_id)
            .limit(1)
        )
    ).first()
    return row is not None


def roles_json(result, role: str | None = None) -> dict:
    """The effective policy per role, as JSON-able dicts.

    Delegates to the SDK's ``effective_policy_by_role`` so the resolve endpoint
    and the compile path serialize a project the same way ``hexgate policy
    resolve`` does — same role order (sorted), same bytes. ``role`` narrows to a
    single role (the resolve endpoint's ``?role=``); ``None`` returns all.
    """
    from hexgate.security import effective_policy_by_role

    return effective_policy_by_role(result, None if role is None else [role])


async def resolved_policy_yaml(
    session: AsyncSession, project_id: str, agent: str = DEFAULT_AGENT
) -> str:
    """One agent's resolved role-keyed policy as inline-roles YAML.

    Serializes to the ``roles:`` shape ``build_signed_bundle`` accepts, so the
    platform compile path is byte-for-byte the same as a single-file policy.
    ``agent`` selects that agent's column of the ``(role, agent)`` matrix (Path A:
    one bundle per agent). Raises the SDK's ``LinkError`` / ``PolicySetError`` /
    ``ConstraintParseError`` if the modules don't compose, so callers can leave
    live bundles untouched.
    """
    from hexgate.security import RESOLVED_POLICY_MARKER

    result = await resolve(session, project_id, agent=agent)
    return yaml.safe_dump(
        {"roles": roles_json(result), RESOLVED_POLICY_MARKER: True}, sort_keys=False
    )


async def resolved_yaml_by_agent(
    session: AsyncSession, project_id: str, agents: Iterable[str]
) -> dict[str, str]:
    """Resolve several agents' policy YAML, loading modules + bindings **once**.

    Compiling a modular project fans out over its agents; resolving each via
    ``resolved_policy_yaml`` would re-read the store and re-parse every module per
    agent. This loads the SDK inputs once and resolves each agent's column in
    memory. Raises the SDK's link/compose errors if any agent's set doesn't
    compose, so callers keep the last-good bundle (fail-safe)."""
    from hexgate.security import RESOLVED_POLICY_MARKER, resolve_for_project

    boundaries, capabilities, roles = await _sdk_inputs(session, project_id)
    out: dict[str, str] = {}
    for agent in agents:
        result = resolve_for_project(boundaries, capabilities, roles, agent=agent)
        out[agent] = yaml.safe_dump(
            {"roles": roles_json(result), RESOLVED_POLICY_MARKER: True},
            sort_keys=False,
        )
    return out


# --- compose file store (entry-file + import graph) --------------------------
# Additive alongside the tier store above: a project may be authored as files —
# one entry ``policy.yaml`` plus imported fragments — resolved through the SDK's
# compose front-end, instead of tier modules + a role binding. ``is_modular`` and
# the compile path prefer the entry file when present.

ENTRY_FILE = "policy.yaml"


async def list_files(session: AsyncSession, project_id: str) -> list[PolicyFile]:
    rows = await session.exec(
        select(PolicyFile)
        .where(PolicyFile.project_id == project_id)
        .order_by(PolicyFile.name)  # type: ignore[arg-type]
    )
    return list(rows.all())


async def get_file(
    session: AsyncSession, project_id: str, name: str
) -> PolicyFile | None:
    return (
        await session.exec(
            select(PolicyFile).where(
                PolicyFile.project_id == project_id, PolicyFile.name == name
            )
        )
    ).first()


async def has_entry_file(session: AsyncSession, project_id: str) -> bool:
    return await get_file(session, project_id, ENTRY_FILE) is not None


async def _files_map(session: AsyncSession, project_id: str) -> dict[str, str]:
    """The project's files as ``{name: content}`` — the compose loader's substrate."""
    return {r.name: r.content for r in await list_files(session, project_id)}


def _db_loader(files: dict[str, str]):
    """A compose ``Loader`` over a ``{name: content}`` map — a missing file raises
    ``FileNotFoundError`` so the resolver reports a source-named ``cannot import``."""

    def load(name: str) -> str:
        try:
            return files[name]
        except KeyError:
            raise FileNotFoundError(name) from None

    return load


def _resolve_files(files: dict[str, str], agent: str = DEFAULT_AGENT):
    """Resolve a ``{name: content}`` project for one agent through compose. Pure —
    no DB; raises the SDK's compose errors (``LinkError`` etc.)."""
    from hexgate.security.compose import resolve_text

    if ENTRY_FILE not in files:
        raise InvalidModuleError(f"no entry file {ENTRY_FILE!r} in this project")
    return resolve_text(
        files[ENTRY_FILE],
        agent=agent,
        source=ENTRY_FILE,
        loader=_db_loader(files),
        entry_path=ENTRY_FILE,
    )


def _compose_agent_names(files: dict[str, str]) -> list[str]:
    """``"*"`` plus every agent the entry declares — the compile fan-out set."""
    from hexgate.security.compose import parse_entry

    entry = parse_entry(files[ENTRY_FILE], source=ENTRY_FILE)
    return [DEFAULT_AGENT, *sorted(entry.agents)]


async def compose_resolve(
    session: AsyncSession, project_id: str, agent: str = DEFAULT_AGENT
):
    """Resolve the project's entry file for one agent via the compose front-end."""
    return _resolve_files(await _files_map(session, project_id), agent)


def validate_compose_file(content: str) -> None:
    """Structural check: the file parses as a compose document (raises
    :class:`InvalidModuleError` → HTTP 422). Checked before resolution so a
    malformed file returns 422, not the 409 an unresolvable-but-valid file gets."""
    from hexgate.security.compose import parse_entry
    from hexgate.security.modules import LinkError

    try:
        parse_entry(content, source="<file>")
    except LinkError as exc:
        raise InvalidModuleError(str(exc)) from exc


async def project_resolves_with_file(
    session: AsyncSession, project_id: str, name: str, content: str
) -> tuple[bool, str | None]:
    """Whether the project still resolves (for every agent) with ``name`` set to
    ``content``. Vacuously true until an entry file exists (a project being built
    up file-by-file); the router turns a False into a 409."""
    files = {**await _files_map(session, project_id), name: content}
    if ENTRY_FILE not in files:
        return True, None
    try:
        for agent in _compose_agent_names(files):
            _resolve_files(files, agent)
    except compose_error_types() as exc:
        return False, str(exc)
    return True, None


async def upsert_file(
    session: AsyncSession, *, project_id: str, name: str, content: str
) -> PolicyFile:
    """Create or replace a file — a store primitive. The router owns validation:
    :func:`validate_compose_file` (→ 422) and :func:`project_resolves_with_file`
    (→ 409) run before this, so it doesn't re-parse the content."""
    chash = _content_hash(content)
    row = await get_file(session, project_id, name)
    if row is None:
        row = PolicyFile(
            id=new_id(PolicyFile),
            project_id=project_id,
            name=name,
            content=content,
            content_hash=chash,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError:  # create race — fall back to update
            await session.rollback()
            row = await get_file(session, project_id, name)
            if row is None:  # the racing row was deleted between — re-create
                row = PolicyFile(
                    id=new_id(PolicyFile),
                    project_id=project_id,
                    name=name,
                    content=content,
                    content_hash=chash,
                )
                session.add(row)
            else:
                row.content, row.content_hash, row.updated_at = (
                    content,
                    chash,
                    utcnow(),
                )
    else:
        row.content, row.content_hash, row.updated_at = content, chash, utcnow()
    await session.commit()
    return row


async def delete_file(session: AsyncSession, *, project_id: str, name: str) -> bool:
    row = await get_file(session, project_id, name)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True


async def compose_check(session: AsyncSession, project_id: str) -> list[dict]:
    """Lints for the compose project: a resolution failure surfaces as one error
    lint. (Rich analyzer lints for the compose model are a follow-up.)"""
    files = await _files_map(session, project_id)
    if ENTRY_FILE not in files:
        return []
    try:
        for agent in _compose_agent_names(files):
            _resolve_files(files, agent)
    except compose_error_types() as exc:
        return [{"code": "link-error", "severity": "error", "message": str(exc)}]
    return []


async def resolve_auto(
    session: AsyncSession, project_id: str, agent: str = DEFAULT_AGENT
):
    """Resolve for the resolve endpoint — compose (an entry file) or the tier
    store, deciding from a **single** store read of the files. Raises the SDK's
    compose errors (:func:`compose_error_types`, a superset of the tier errors)."""
    files = await _files_map(session, project_id)
    if ENTRY_FILE in files:
        return _resolve_files(files, agent)
    return await resolve(session, project_id, agent=agent)


async def check_auto(session: AsyncSession, project_id: str) -> list[dict]:
    """Lints for the check endpoint — compose (resolution errors) or tier (analyzer
    lints), from a single files read. Returns uniform ``PolicyLintOut``-shaped
    dicts so the router builds them the same way for both stores."""
    files = await _files_map(session, project_id)
    if ENTRY_FILE in files:
        try:
            for agent in _compose_agent_names(files):
                _resolve_files(files, agent)
        except compose_error_types() as exc:
            return [
                {
                    "code": "link-error",
                    "severity": "error",
                    "message": str(exc),
                    "source": None,
                    "tier": None,
                    "tool": None,
                    "role": None,
                }
            ]
        return []
    return [
        {
            "code": lint.code,
            "severity": lint.severity,
            "message": lint.message,
            "source": lint.source,
            "tier": lint.tier,
            "tool": lint.tool,
            "role": lint.role,
        }
        for lint in await check(session, project_id)
    ]


async def compose_preview(
    session: AsyncSession,
    project_id: str,
    *,
    name: str,
    content: str,
    agent: str = DEFAULT_AGENT,
) -> dict:
    """Resolve the project with a draft ``name``=``content`` overlaid — the
    editor's live preview. Returns the requested agent's effective policy, or an
    error lint. Validates *every* declared agent (like the save-time 409 check) so
    the preview and the save agree — a draft that breaks a different agent still
    previews as an error, not falsely clean."""
    files = {**await _files_map(session, project_id), name: content}
    if ENTRY_FILE not in files:
        return {
            "resolved": None,
            "lints": [
                {
                    "code": "link-error",
                    "severity": "error",
                    "message": f"no entry file {ENTRY_FILE!r} in this project",
                }
            ],
        }
    try:
        for a in _compose_agent_names(files):
            _resolve_files(files, a)  # validate every declared agent
        requested = _resolve_files(files, agent)  # the one to return
    except compose_error_types() as exc:
        return {
            "resolved": None,
            "lints": [{"code": "link-error", "severity": "error", "message": str(exc)}],
        }
    return {"resolved": roles_json(requested), "lints": []}


def _graph_from(agent_names, resolve_for_agent, role: str | None = None) -> dict:
    """Build the node/edge graph from a per-agent resolver — model-agnostic, fed
    either the compose or the tier resolver. An agent's effective tools split into
    tool *calls*, lowered *reach* keys (``agent.tool:``/``agent.handoff:`` → an
    agent→agent edge tagged by ``via``), and the *admission* key (``agent.run`` → a
    role→agent ingress edge); a ``mcp-`` tool is an ``mcp`` node. ``role`` filters
    to one role, else unions across every role each agent resolves. Nodes/edges
    de-dup: an edge keeps the strictest verdict (deny > approval_required > allow),
    unions its constraints, and records the roles it appears under."""
    from hexgate.security import DEFAULT_ROLE_NAME
    from hexgate.security.models import (
        AGENT_REACH_PREFIXES,
        AGENT_RUN_TOOL,
        is_agent_reach_key,
    )

    _RANK = {"deny": 3, "approval_required": 2, "allow": 1}
    nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}

    def add_node(node_id: str, kind: str, label: str) -> None:
        nodes.setdefault(node_id, {"id": node_id, "kind": kind, "label": label})

    def add_edge(source, target, kind, verdict, constraints, via=None, at_role=None):
        prev = edges.get((source, target, kind, via))
        if prev is None:
            edges[(source, target, kind, via)] = {
                "source": source,
                "target": target,
                "kind": kind,
                "via": via,
                "verdict": verdict,
                "constraints": list(constraints),
                "roles": [at_role] if at_role else [],
            }
            return
        if _RANK.get(verdict, 0) > _RANK.get(prev["verdict"], 0):
            prev["verdict"] = verdict
        for c in constraints:
            if c not in prev["constraints"]:
                prev["constraints"].append(c)
        if at_role and at_role not in prev["roles"]:
            prev["roles"].append(at_role)

    for agent in sorted(agent_names):
        add_node(f"agent:{agent}", "agent", agent)
        result = resolve_for_agent(agent)
        # In the union view (role=None) the same edge can appear under several
        # roles; we keep the strictest verdict and union the constraints, so a
        # merged edge can be more restrictive than any single role. The per-edge
        # ``roles`` list (and the ?role= filter) give the exact per-role picture.
        roles_here = [role] if role is not None else list(result.by_role)
        for r in roles_here:
            link_result = result.by_role.get(r)
            if link_result is None:
                continue
            policy = link_result.effective[DEFAULT_ROLE_NAME]
            for tool, tp in policy.effective_tools.items():
                cons = list(tp.constraints)
                if tool == AGENT_RUN_TOOL:
                    add_node(f"role:{r}", "role", r)
                    add_edge(
                        f"role:{r}",
                        f"agent:{agent}",
                        "admission",
                        tp.mode,
                        cons,
                        at_role=r,
                    )
                elif is_agent_reach_key(tool):
                    prefix = next(p for p in AGENT_REACH_PREFIXES if tool.startswith(p))
                    via = prefix[len("agent.") : -1]  # "tool" | "handoff"
                    target = tool[len(prefix) :]
                    add_node(f"agent:{target}", "agent", target)
                    add_edge(
                        f"agent:{agent}",
                        f"agent:{target}",
                        "reach",
                        tp.mode,
                        cons,
                        via=via,
                        at_role=r,
                    )
                else:
                    is_mcp = tool.startswith("mcp-")
                    kind = "mcp" if is_mcp else "tool"
                    # Drop the "mcp-" prefix in the label so "mcp-demo-read" reads
                    # as "demo-read".
                    node_label = tool[len("mcp-") :] if is_mcp else tool
                    add_node(f"tool:{tool}", kind, node_label)
                    add_edge(
                        f"agent:{agent}",
                        f"tool:{tool}",
                        "call",
                        tp.mode,
                        cons,
                        at_role=r,
                    )

    # "*" is the generic-column sentinel, not a real agent — drop its node when it
    # carries nothing (named agents present, no top-level grants) so the UI doesn't
    # render a literal "*" agent; keep it when a top-level grant gives it an edge.
    star = f"agent:{DEFAULT_AGENT}"
    if star in nodes and not any(
        e["source"] == star or e["target"] == star for e in edges.values()
    ):
        del nodes[star]

    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


async def policy_graph(
    session: AsyncSession, project_id: str, role: str | None = None
) -> dict:
    """A node/edge graph of the project's agents, tools, and reach/admission edges,
    for one ``role`` (or the union across roles). Auto-dispatches compose (an entry
    file) vs the tier store — a single files read — and feeds the same builder.
    Raises the SDK's compose errors if the project doesn't resolve."""
    files = await _files_map(session, project_id)
    if ENTRY_FILE in files:
        agent_names = _compose_agent_names(files)

        def resolve_for_agent(a: str):
            return _resolve_files(files, a)
    else:
        from hexgate.security import resolve_for_project

        boundaries, capabilities, roles_matrix = await _sdk_inputs(session, project_id)
        agent_names = sorted(_all_agent_names(roles_matrix))

        def resolve_for_agent(a: str):
            return resolve_for_project(boundaries, capabilities, roles_matrix, agent=a)

    return _graph_from(agent_names, resolve_for_agent, role=role)


async def test_policy(
    session: AsyncSession,
    project_id: str,
    *,
    role: str,
    tool: str,
    agent: str = DEFAULT_AGENT,
    args: dict,
    attributes: dict | None = None,
    draft: tuple[str, str] | None = None,
):
    """Evaluate one tool call against the resolved policy for ``role`` + ``agent``,
    then run the SDK decision engine.

    ``draft`` is an optional ``(name, content)`` compose overlay — the editor's
    unsaved edit. It's applied to the file set, so it takes effect whenever the
    project resolves via compose (has, or the draft introduces, an entry file); a
    project with no entry file resolves from the tier store and a draft naming a
    non-entry file has no tier meaning. Raises the SDK's compose errors if the
    project doesn't resolve, and :class:`UnknownRoleError` (→ 404) for an unknown
    role."""
    files = await _files_map(session, project_id)
    if draft is not None:
        files = {**files, draft[0]: draft[1]}
    if ENTRY_FILE in files:
        result = _resolve_files(files, agent)
    else:
        from hexgate.security import resolve_for_project

        boundaries, capabilities, roles = await _sdk_inputs(session, project_id)
        result = resolve_for_project(boundaries, capabilities, roles, agent=agent)
    if role not in result.policy_set.roles:
        raise UnknownRoleError(role)
    return result.policy_set.evaluate(
        role=role, tool=tool, args=dict(args), attributes=attributes
    )


async def resolved_yaml_by_agent_compose(
    session: AsyncSession, project_id: str, agents: Iterable[str]
) -> dict[str, str]:
    """Per-agent resolved YAML for the compile fan-out, via compose. Reads the
    store once; note it re-parses per agent (the compose resolver runs per-agent
    import resolution, which mutates per-scope state, so the parsed entry can't be
    safely shared across agents — a shared parse cache is a follow-up). Raises the
    SDK's compose errors so callers keep the last-good bundle (fail-safe)."""
    from hexgate.security import RESOLVED_POLICY_MARKER

    files = await _files_map(session, project_id)
    out: dict[str, str] = {}
    for agent in agents:
        result = _resolve_files(files, agent)
        out[agent] = yaml.safe_dump(
            {"roles": roles_json(result), RESOLVED_POLICY_MARKER: True},
            sort_keys=False,
        )
    return out


# --- compile dispatch: entry-file (compose) vs tier store --------------------
# The compile path calls these; they pick the compose resolver when the project
# has a `policy.yaml` entry, else fall back to the tier resolver. One branch, so
# `agents.service` stays store-shape-agnostic.


async def resolved_yaml_by_agent_auto(
    session: AsyncSession, project_id: str, agents: Iterable[str]
) -> dict[str, str]:
    """Per-agent resolved YAML for compile — compose if there's an entry file,
    else tier. Raises the SDK's compose errors so callers keep the last bundle."""
    if await has_entry_file(session, project_id):
        return await resolved_yaml_by_agent_compose(session, project_id, agents)
    return await resolved_yaml_by_agent(session, project_id, agents)


async def resolved_policy_yaml_auto(
    session: AsyncSession, project_id: str, agent: str = DEFAULT_AGENT
) -> str:
    """One agent's resolved YAML for compile — compose if there's an entry file,
    else tier."""
    if not await has_entry_file(session, project_id):
        return await resolved_policy_yaml(session, project_id, agent)
    from hexgate.security import RESOLVED_POLICY_MARKER

    result = await compose_resolve(session, project_id, agent)
    return yaml.safe_dump(
        {"roles": roles_json(result), RESOLVED_POLICY_MARKER: True}, sort_keys=False
    )
