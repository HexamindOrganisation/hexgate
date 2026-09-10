"""Agent endpoints — dashboard CRUD/read (cookie) + SDK register/read (bearer).

``keystore.sign`` is read lazily from :mod:`hexgate_api.main` at call time in the
two routes that compile+sign a bundle (update, register).
"""

import base64
import hashlib
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.core.db import get_session
from hexgate_api.deps.org import require_org_member
from hexgate_api.deps.tokens import require_project
from hexgate_api.models import Agent, AgentVersion
from hexgate_api.schemas import (
    AgentManifest,
    AgentManifestView,
    AgentRead,
    AgentUpdate,
    PolicyValidationError,
    RegisterAgentRequest,
    RegisterAgentResponse,
    ValidatePolicyRequest,
    ValidatePolicyResponse,
)
from hexgate_api.features.agents.service import (
    get_agent,
    get_latest_agent_versions_map,
    list_agents,
    register_manifest,
    update_agent,
)
from hexgate_api.seeds.defaults import ensure_default_project

if TYPE_CHECKING:
    from hexgate.security.policy_set import PolicySet

router = APIRouter()


def _agent_read(agent: Agent) -> AgentRead:
    """Shared serialiser used by GET, list, and PUT — keeps the wire format aligned."""
    return AgentRead(
        id=agent.id,
        name=agent.name,
        agent_yaml=agent.agent_yaml,
        policy_yaml=agent.policy_yaml,
        system_md=agent.system_md,
        updated_at=agent.updated_at,
        bundle_wasm_b64=(
            base64.b64encode(agent.compiled_wasm).decode("ascii")
            if agent.compiled_wasm is not None
            else None
        ),
        bundle_manifest=agent.bundle_manifest,
        bundle_signature_b64=(
            base64.b64encode(agent.bundle_signature).decode("ascii")
            if agent.bundle_signature is not None
            else None
        ),
    )


@router.get(
    "/projects/{project_id}/agents",
    response_model=list[AgentRead],
    dependencies=[Depends(require_org_member)],
)
async def api_list_agents(
    project_id: str, session: AsyncSession = Depends(get_session)
) -> list[AgentRead]:
    await ensure_default_project(session)
    return [_agent_read(a) for a in await list_agents(session, project_id)]


def _build_agent_manifest_view(
    agent: Agent, agent_version: AgentVersion | None
) -> AgentManifestView:
    """Build the dashboard manifest envelope from an Agent + its latest version.

    Rehydrates ``AgentVersion.manifest`` (a JSON snapshot, validated against
    :class:`AgentManifest` at registration time) back into the typed shape,
    or returns the envelope with ``manifest=None`` when no usable version
    exists — either no version row at all, or a row whose ``manifest`` column
    is NULL (nullable on the model; only reachable via direct DB writes).
    """
    if agent_version is None or agent_version.manifest is None:
        return AgentManifestView(name=agent.name, updated_at=agent.updated_at)
    return AgentManifestView(
        name=agent.name,
        manifest=AgentManifest.model_validate(agent_version.manifest),
        version=agent_version.version,
        content_hash=agent_version.content_hash,
        updated_at=agent_version.created_at,
    )


# Declared before the ``/agents/{name}`` route so FastAPI matches the literal
# ``manifest`` segment instead of binding it as a name path parameter.
@router.get(
    "/projects/{project_id}/agents/manifest",
    response_model=list[AgentManifestView],
    dependencies=[Depends(require_org_member)],
)
async def api_list_agent_manifests(
    project_id: str, session: AsyncSession = Depends(get_session)
) -> list[AgentManifestView]:
    """Bulk read of every agent's latest registered manifest.

    One row per Agent. Agents that exist but have no version registered
    come back with ``manifest=None``.
    """
    await ensure_default_project(session)
    agents = await list_agents(session, project_id)
    latest_by_agent = await get_latest_agent_versions_map(
        session, [a.id for a in agents]
    )
    return [
        _build_agent_manifest_view(agent, latest_by_agent.get(agent.id))
        for agent in agents
    ]


@router.get(
    "/projects/{project_id}/agents/{name}",
    response_model=AgentRead,
    dependencies=[Depends(require_org_member)],
)
async def api_get_agent(
    project_id: str,
    name: str,
    response: Response,
    session: AsyncSession = Depends(get_session),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> AgentRead | Response:
    """Return an agent's YAMLs + signed bundle (dashboard read path).

    Cookie-authed via :func:`require_org_member`. The SDK's bearer-only
    equivalent is :func:`api_get_agent_by_token` at ``GET
    /v1/agents/{name}`` — same response shape, project derived from
    the token instead of the URL.

    Supports ETag-based conditional GETs: the response carries the
    bundle's ``wasm_hash`` as an ``ETag`` header. A subsequent request
    with ``If-None-Match: <wasm_hash>`` returns ``304 Not Modified``
    when the bundle hasn't changed.
    """
    await ensure_default_project(session)
    agent = await get_agent(session, project_id, name)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")

    # ETag is a quoted opaque string per RFC 7232. We use the wasm_hash
    # (a sha256 hex digest); falls back to None when no bundle is stored.
    bundle_hash = (
        hashlib.sha256(agent.compiled_wasm).hexdigest()
        if agent.compiled_wasm is not None
        else None
    )
    etag = f'"{bundle_hash}"' if bundle_hash else None

    if etag and if_none_match and if_none_match.strip() == etag:
        # 304 — no body, just the ETag so the client can re-confirm.
        return Response(status_code=304, headers={"ETag": etag})

    if etag:
        response.headers["ETag"] = etag
    return _agent_read(agent)


@router.put(
    "/projects/{project_id}/agents/{name}",
    response_model=AgentRead,
    dependencies=[Depends(require_org_member)],
)
async def api_update_agent(
    project_id: str,
    name: str,
    body: AgentUpdate,
    session: AsyncSession = Depends(get_session),
) -> AgentRead:
    from hexgate_api.core.keystore import keystore
    from hexgate_api.core.locks import project_lock

    await ensure_default_project(session)
    if body.policy_yaml is not None:
        _reject_unloadable_policy(body.policy_yaml)
    # Hold the project lock: this compiles + writes a bundle, so it must not
    # interleave with recompile_project (a concurrent policy write) and commit
    # out of order. See core.locks.
    async with project_lock(project_id):
        agent = await update_agent(
            session,
            project_id,
            name,
            agent_yaml=body.agent_yaml,
            policy_yaml=body.policy_yaml,
            system_md=body.system_md,
            # Compile + sign the policy into a WASM bundle at save time, using
            # the platform's root key (same key that signs biscuits).
            sign=keystore.sign,
        )
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return _agent_read(agent)


@router.post(
    "/projects/{project_id}/agents/{name}/validate",
    response_model=ValidatePolicyResponse,
    dependencies=[Depends(require_org_member)],
)
async def api_validate_policy(
    project_id: str,  # noqa: ARG001 — routed by FastAPI, scope-checked by future auth
    name: str,  # noqa: ARG001 — same
    body: ValidatePolicyRequest,
) -> ValidatePolicyResponse:
    """Parse ``policy.yaml`` end-to-end + check every ``constraints`` string.

    Server-side validation keeps one source of truth on grammar — the same
    parsers the SDK enforces with at run time. See
    :func:`_validate_policy_document` for what the pass covers; the save route
    runs the identical check, so a policy that validates here is one that saves
    and a policy that saves is one that validated.
    """
    return _validate_policy_document(body.policy_yaml)


# A policy the loader rejects is the author's mistake, not a server fault, and
# it is not a request-shape error FastAPI could have caught — 422 is the code
# the /validate route's own contract already implies for a document it fails.
POLICY_REJECTED_STATUS = 422


def _reject_unloadable_policy(policy_yaml: str) -> None:
    """Refuse a save whose policy would compile to nothing.

    ``compile_bundle`` degrades *every* compile failure to "store no bundle",
    which is right when ``opa`` is absent but wrong for a broken document: the
    save returned 200, the agent's three bundle columns were nulled, and
    enforcement silently dropped to the SDK's pydantic fallback with no
    diagnostic on the wire. Gate on the same check ``/validate`` runs so the two
    routes cannot disagree about which documents are loadable, and so a typo in
    a fence is a failed request instead of a quietly unenforced policy.
    """
    result = _validate_policy_document(policy_yaml)
    if result.ok:
        return
    raise HTTPException(
        status_code=POLICY_REJECTED_STATUS,
        detail={
            "message": "policy_yaml did not validate; nothing was saved",
            "errors": [error.model_dump() for error in result.errors],
        },
    )


def _validate_policy_document(policy_yaml: str) -> ValidatePolicyResponse:
    """Validate a policy document the whole way down. Handles both shapes:

    * flat single-policy document → validated as one :class:`AgentPolicy`
    * inline-roles document (top-level ``roles:`` map) → each entry
      validated as an :class:`AgentPolicy`, then every constraint inside
      every tool is parsed against the M1 grammar.

    Either way the document then goes through :func:`_load_document`, the same
    loader the compiler and the SDK use, so ``ok`` can't report a document
    those two reject — see that function for what the per-role pass misses.

    Returns a flat list of ``{role, line, message}`` diagnostics. ``role``
    is ``None`` for top-level YAML / schema errors; populated when the
    failure lives inside a specific role's section.

    ``warnings`` carries authoring lints — grants reachable only through the
    ``default`` fallback, i.e. by any caller carrying an undefined role name.
    They never set ``ok`` to False.
    """
    import yaml
    from pydantic import ValidationError
    from yaml.error import MarkedYAMLError

    from hexgate.security import AgentPolicy
    from hexgate.security.constraints import (
        ConstraintParseError,
        parse_constraint,
    )

    errors: list[PolicyValidationError] = []
    try:
        parsed = yaml.safe_load(policy_yaml) or {}
    except MarkedYAMLError as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark else None
        return ValidatePolicyResponse(
            ok=False,
            errors=[
                PolicyValidationError(
                    line=line,
                    message=f"YAML parse: {exc.problem or exc}",
                )
            ],
        )

    # Valid YAML that isn't a mapping (a bare list, a scalar) parses fine and
    # then has no keys to walk — report it rather than letting every `.get`
    # below raise.
    if not isinstance(parsed, dict):
        return ValidatePolicyResponse(
            ok=False,
            errors=[
                PolicyValidationError(
                    message=(
                        f"policy must be a YAML mapping, got {type(parsed).__name__}"
                    )
                )
            ],
        )

    def _check_policy(policy: AgentPolicy, role_name: str | None) -> None:
        for tool_name, tool_policy in policy.tools.items():
            for constraint in tool_policy.constraints:
                try:
                    parse_constraint(constraint)
                except ConstraintParseError as exc:
                    errors.append(
                        PolicyValidationError(
                            role=role_name,
                            message=f"{tool_name}: {exc}",
                        )
                    )

    if isinstance(parsed.get("roles"), dict):
        # Inline-roles shape: validate each role's policy.
        for role_name, role_spec in parsed["roles"].items():
            try:
                role_policy = AgentPolicy.model_validate(role_spec or {})
            except ValidationError as exc:
                errors.append(
                    PolicyValidationError(
                        role=role_name,
                        message=f"policy schema: {exc.errors()[0]['msg']}",
                    )
                )
                continue
            _check_policy(role_policy, role_name)
    else:
        # Flat single-policy shape.
        try:
            policy = AgentPolicy.model_validate(parsed)
        except ValidationError as exc:
            return ValidatePolicyResponse(
                ok=False,
                errors=[
                    PolicyValidationError(
                        message=f"policy schema: {exc.errors()[0]['msg']}",
                    )
                ],
            )
        _check_policy(policy, None)

    policy_set, document_error = _load_document(parsed)
    # Only when the per-role pass found nothing: the loader fails on the same
    # cause, so reporting it too would just duplicate a diagnostic that already
    # names the offending role.
    if document_error is not None and not errors:
        errors.append(document_error)

    return ValidatePolicyResponse(
        ok=not errors,
        errors=errors,
        warnings=[] if errors else _default_role_warnings(policy_set),
    )


def _load_document(
    parsed: dict,
) -> "tuple[PolicySet | None, PolicyValidationError | None]":
    """Load the document the way the SDK does at run time — the final gate.

    Returns the loaded set for the lints below, or the failure to report. The
    per-role pass validates each role in isolation, so it never sees file-level
    grammar (an unknown sibling of ``roles:``, a malformed top-level
    ``constraints:`` block) or a failure needing the resolved set (inheritance,
    mixins, ``consts`` references). Reporting these keeps ``ok`` from
    disagreeing with the loader the compiler and the SDK both go through — a
    document that validates here has to be one they accept.
    """
    from pydantic import ValidationError

    from hexgate.security.policy_set import (
        PolicySetError,
        load_policy_set_from_dict,
    )

    try:
        return load_policy_set_from_dict(parsed), None
    except PolicySetError as exc:
        return None, PolicyValidationError(message=str(exc))
    except ValidationError as exc:
        return None, PolicyValidationError(
            message=f"policy schema: {exc.errors()[0]['msg']}"
        )


def _default_role_warnings(
    policy_set: "PolicySet | None",
) -> list[PolicyValidationError]:
    """Authoring lints over the document as a whole.

    Needs a fully loaded :class:`PolicySet` — inheritance and mixins resolved —
    which the per-role checks don't build, validating roles in isolation.
    """
    if policy_set is None:
        return []

    from hexgate.security.analyzer import check_default_role_exposure

    return [
        PolicyValidationError(
            role=lint.role, tool=lint.tool, message=f"{lint.code}: {lint.message}"
        )
        for lint in check_default_role_exposure(policy_set)
    ]


@router.post("/agents", response_model=RegisterAgentResponse)
async def api_register_agent(
    body: RegisterAgentRequest,
    response: Response,
    project_id: str = Depends(require_project),
    session: AsyncSession = Depends(get_session),
) -> RegisterAgentResponse:
    """SDK-facing: register/upsert an agent manifest under the bearer's project.

    Threads ``keystore.sign`` through so first-time registers get a real
    signed WASM bundle (and a starter role-aware policy) — re-registers
    don't touch the agent's policy_yaml, so the operator's dashboard
    edits are preserved.
    """
    from hexgate_api.core.keystore import keystore
    from hexgate_api.core.locks import project_lock
    from hexgate_api.features.agents.service import get_agent

    # Only a FIRST registration compiles + writes a bundle; re-registering an
    # existing agent just snapshots the manifest (no compile). Take the project
    # lock only in that case — otherwise a fleet redeploy's no-op re-registers
    # (`hexgate serve` auto-registers on startup) would all queue behind the lock
    # and any in-flight recompile for nothing.
    if await get_agent(session, project_id, body.manifest.name) is None:
        async with project_lock(project_id):
            version, created = await register_manifest(
                session, project_id, body.manifest, sign=keystore.sign
            )
    else:
        version, created = await register_manifest(
            session, project_id, body.manifest, sign=keystore.sign
        )
    response.status_code = 201 if created else 200
    return RegisterAgentResponse(
        agent_id=version.agent_id,
        agent_version_id=version.id,
        name=body.manifest.name,
        version=version.version,
        content_hash=version.content_hash,
        created=created,
    )


@router.get("/agents/{name}", response_model=AgentRead)
async def api_get_agent_by_token(
    name: str,
    response: Response,
    project_id: str = Depends(require_project),
    session: AsyncSession = Depends(get_session),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> AgentRead | Response:
    """SDK-facing read of an agent — project comes from the bearer token.

    The cookie-authed dashboard counterpart at
    ``GET /v1/projects/{id}/agents/{name}`` returns the same shape;
    this route is the CLI's policy-refresh entry point and is
    bearer-only via :func:`require_project`.

    ETag semantics mirror the cookie route — the SDK's per-run
    conditional GET (``If-None-Match: <wasm_hash>`` → ``304``) costs
    one short round-trip when the bundle hasn't changed.
    """
    await ensure_default_project(session)
    agent = await get_agent(session, project_id, name)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")

    bundle_hash = (
        hashlib.sha256(agent.compiled_wasm).hexdigest()
        if agent.compiled_wasm is not None
        else None
    )
    etag = f'"{bundle_hash}"' if bundle_hash else None

    if etag and if_none_match and if_none_match.strip() == etag:
        return Response(status_code=304, headers={"ETag": etag})

    if etag:
        response.headers["ETag"] = etag
    return _agent_read(agent)
