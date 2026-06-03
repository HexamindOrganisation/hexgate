"""Tests for the inline-roles policy shape on the platform API.

After phase 4c-A the per-agent storage collapsed to three string columns —
``agent_yaml``, ``policy_yaml``, ``system_md``. Role bundles live inline
under a top-level ``roles:`` key in ``policy_yaml`` rather than in a
separate ``roles_json`` column. These tests cover the new shape:

  * seeded support_bot's policy.yaml carries inline roles
  * GET / list / PUT round-trip the three string fields cleanly
  * /validate accepts a single policy_yaml document (flat or inline-roles)
    and surfaces YAML / schema / constraint errors with role attribution
"""

from __future__ import annotations

import pytest_asyncio
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import main
from main import app
from services import DEFAULT_PROJECT_ID, DEFAULT_USER_ID, ensure_default_project


@pytest_asyncio.fixture
async def session_factory():
    """A fresh in-memory async engine + session factory.

    StaticPool keeps every Session bound to the same connection — otherwise
    each :memory: handle gets its own private DB and the tables vanish.
    Returns the factory so tests + the client fixture can both build
    sessions against the same in-memory DB.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as bootstrap:
        await ensure_default_project(bootstrap)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def client(session_factory, tmp_path) -> TestClient:
    """TestClient that routes ``get_session`` through the shared test factory."""
    from keystore import FileKeyStore

    async def override_session():
        async with session_factory() as session:
            yield session

    # Endpoints use the local main.get_session, so we override that one.
    app.dependency_overrides[main.get_session] = override_session
    # PUT /agents now compiles + signs the policy bundle, so the endpoint
    # needs an initialised keystore. The fixture doesn't run the app
    # lifespan, so wire up a throwaway temp-dir keystore here and restore
    # the original afterward.
    original_keystore = main.keystore
    main.keystore = FileKeyStore(base_dir=tmp_path / "keystore")
    main.keystore.ensure_keypair()
    try:
        # Bake the dev-user header into every request so M3 Phase 2's
        # require_org_member dependency lets these tests through. The
        # default seed user is a member of support-bot's org.
        yield TestClient(app, headers={"X-Dev-User": DEFAULT_USER_ID})
    finally:
        app.dependency_overrides.clear()
        main.keystore = original_keystore


# ---------------------------------------------------------------------------
# Seed shape
# ---------------------------------------------------------------------------


def test_support_bot_policy_carries_inline_roles(client: TestClient) -> None:
    """The seed plants four inline roles inside support_bot's policy.yaml."""
    resp = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot")
    assert resp.status_code == 200
    body = resp.json()
    # Wire format is three string fields — no roles map at the top level.
    assert set(body.keys()) >= {"agent_yaml", "policy_yaml", "system_md"}
    assert "roles" not in body
    # Roles live inline in policy.yaml.
    parsed = yaml.safe_load(body["policy_yaml"])
    assert isinstance(parsed.get("roles"), dict)
    assert set(parsed["roles"].keys()) == {
        "read_only",
        "default",
        "support",
        "billing",
    }
    # The mixin marker survives the round-trip.
    assert parsed["roles"]["read_only"].get("is_mixin") is True


def test_default_agent_policy_stays_flat(client: TestClient) -> None:
    """Legacy seed agents keep the single-policy shape — no ``roles:`` key."""
    resp = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/default")
    assert resp.status_code == 200
    parsed = yaml.safe_load(resp.json()["policy_yaml"])
    assert "roles" not in parsed
    assert "tools" in parsed


# ---------------------------------------------------------------------------
# PUT / GET round-trip
# ---------------------------------------------------------------------------


def test_put_agent_updates_policy_yaml(client: TestClient) -> None:
    """PUT updates policy_yaml; subsequent GET reflects the change."""
    new_policy = (
        "version: 1\n"
        "roles:\n"
        "  default:\n"
        "    tools:\n"
        "      refund_order: { mode: allow }\n"
    )
    resp = client.put(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot",
        json={"policy_yaml": new_policy},
    )
    assert resp.status_code == 200
    assert resp.json()["policy_yaml"] == new_policy

    resp = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot")
    assert resp.json()["policy_yaml"] == new_policy


def test_put_agent_partial_update_preserves_other_fields(
    client: TestClient,
) -> None:
    """An update touching one field leaves the others alone."""
    before = client.get(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot"
    ).json()

    resp = client.put(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot",
        json={"system_md": "Updated prompt."},
    )
    assert resp.status_code == 200
    after = resp.json()
    assert after["system_md"] == "Updated prompt."
    assert after["policy_yaml"] == before["policy_yaml"]
    assert after["agent_yaml"] == before["agent_yaml"]


def test_list_agents_returns_three_string_fields(client: TestClient) -> None:
    """The /agents collection endpoint returns the same three-field shape."""
    resp = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents")
    assert resp.status_code == 200
    for agent in resp.json():
        assert "agent_yaml" in agent
        assert "policy_yaml" in agent
        assert "system_md" in agent
        assert "roles" not in agent


# ---------------------------------------------------------------------------
# /agents/{name}/validate — single-document linter
# ---------------------------------------------------------------------------


def test_validate_inline_roles_clean(client: TestClient) -> None:
    """A well-formed inline-roles document → ok=True, no errors."""
    resp = client.post(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot/validate",
        json={
            "policy_yaml": (
                "version: 1\n"
                "roles:\n"
                "  default:\n"
                "    tools:\n"
                "      refund_order:\n"
                "        mode: allow\n"
                "        constraints:\n"
                "          - args.amount <= 50\n"
            )
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "errors": []}


def test_validate_flat_single_policy_clean(client: TestClient) -> None:
    """A legacy single-policy document also validates."""
    resp = client.post(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/default/validate",
        json={
            "policy_yaml": (
                "version: 1\n"
                "default_policy: { mode: deny }\n"
                "tools:\n"
                "  web_search: { mode: allow }\n"
            )
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "errors": []}


def test_validate_reports_yaml_parse_error_with_line(client: TestClient) -> None:
    """A YAML lex error surfaces with the offending line; ``role`` is None."""
    resp = client.post(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot/validate",
        json={"policy_yaml": "tools: [bad: unclosed\n"},
    )
    body = resp.json()
    assert body["ok"] is False
    [err] = body["errors"]
    assert err["role"] is None
    assert err["line"] is not None and err["line"] >= 1
    assert "YAML parse" in err["message"]


def test_validate_reports_constraint_grammar_error_inside_role(
    client: TestClient,
) -> None:
    """An unsupported constraint operator is attributed to its role."""
    resp = client.post(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot/validate",
        json={
            "policy_yaml": (
                "version: 1\n"
                "roles:\n"
                "  billing:\n"
                "    tools:\n"
                "      refund_order:\n"
                "        mode: allow\n"
                "        constraints:\n"
                "          - args.amount ~~ 50\n"
            )
        },
    )
    body = resp.json()
    assert body["ok"] is False
    [err] = body["errors"]
    assert err["role"] == "billing"
    assert "no recognised operator" in err["message"]


def test_validate_accumulates_errors_across_roles(client: TestClient) -> None:
    """Multiple bad roles → multiple diagnostics, one per failure."""
    resp = client.post(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot/validate",
        json={
            "policy_yaml": (
                "version: 1\n"
                "roles:\n"
                "  good:\n"
                "    tools:\n"
                "      refund_order: { mode: allow }\n"
                "  bad1:\n"
                "    tools:\n"
                "      refund_order:\n"
                "        mode: allow\n"
                "        constraints:\n"
                "          - args.amount %%% 50\n"
                "  bad2:\n"
                "    tools:\n"
                "      refund_order:\n"
                "        mode: allow\n"
                "        constraints:\n"
                "          - args.amount @@@ 50\n"
            )
        },
    )
    body = resp.json()
    assert body["ok"] is False
    roles_with_errors = {e["role"] for e in body["errors"]}
    assert roles_with_errors == {"bad1", "bad2"}


# ---------------------------------------------------------------------------
# GET /agents/manifest — dashboard read path rehydrated from the JSON snapshot
# stored on the latest AgentVersion.manifest (not from joined Tool rows).
# ---------------------------------------------------------------------------


def _sample_manifest(
    name: str,
    *,
    description: str | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
) -> dict:
    """Minimal AgentManifest payload for register_manifest in tests."""
    return {
        "name": name,
        "description": description,
        "framework": "fortify",
        "model": model,
        "system_prompt": system_prompt,
        "tools": [
            {
                "name": "echo",
                "description": "Repeat back the input.",
                "input_schema": {
                    "properties": {"msg": {"title": "Message", "type": "string"}},
                    "required": ["msg"],
                },
            }
        ],
    }


def test_manifest_endpoint_returns_envelope_for_unregistered_agents(
    client: TestClient,
) -> None:
    """Seeded agents have YAML but no AgentVersion → manifest is null."""
    resp = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/manifest")
    assert resp.status_code == 200
    rows = resp.json()
    by_name = {row["name"]: row for row in rows}
    assert {"default", "support_bot"} <= set(by_name)
    for row in by_name.values():
        assert row["manifest"] is None
        assert row["version"] is None
        assert row["content_hash"] is None


async def test_manifest_endpoint_returns_registered_manifest_with_tools(
    client: TestClient, session_factory
) -> None:
    """After register_manifest, the endpoint surfaces the full manifest."""
    from schemas import AgentManifest
    from services import register_manifest

    async with session_factory() as session:
        manifest = AgentManifest.model_validate(
            _sample_manifest("support_bot", description="customer support")
        )
        await register_manifest(session, DEFAULT_PROJECT_ID, manifest)

    resp = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/manifest")
    row = next(r for r in resp.json() if r["name"] == "support_bot")
    assert row["version"] == 1
    assert row["content_hash"]
    assert row["manifest"]["description"] == "customer support"
    assert [t["name"] for t in row["manifest"]["tools"]] == ["echo"]
    assert row["manifest"]["tools"][0]["input_schema"]["required"] == ["msg"]


async def test_manifest_endpoint_returns_latest_version(
    client: TestClient, session_factory
) -> None:
    """When multiple versions exist, only the highest one is returned."""
    from schemas import AgentManifest
    from services import register_manifest

    async with session_factory() as session:
        v1 = AgentManifest.model_validate(_sample_manifest("support_bot"))
        v2 = AgentManifest.model_validate(
            {**_sample_manifest("support_bot"), "description": "v2"}
        )
        await register_manifest(session, DEFAULT_PROJECT_ID, v1)
        await register_manifest(session, DEFAULT_PROJECT_ID, v2)

    resp = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/manifest")
    row = next(r for r in resp.json() if r["name"] == "support_bot")
    assert row["version"] == 2
    assert row["manifest"]["description"] == "v2"


async def test_manifest_endpoint_round_trips_model_and_system_prompt(
    client: TestClient, session_factory
) -> None:
    """The new manifest fields survive register → read."""
    from schemas import AgentManifest
    from services import register_manifest

    async with session_factory() as session:
        manifest = AgentManifest.model_validate(
            _sample_manifest(
                "support_bot",
                model="gpt-4o-mini",
                system_prompt="be helpful",
            )
        )
        await register_manifest(session, DEFAULT_PROJECT_ID, manifest)

    resp = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/manifest")
    row = next(r for r in resp.json() if r["name"] == "support_bot")
    assert row["manifest"]["model"] == "gpt-4o-mini"
    assert row["manifest"]["system_prompt"] == "be helpful"


def test_manifest_endpoint_unregistered_agents_have_no_leakage(
    client: TestClient,
) -> None:
    """Agents without a registered version expose a null manifest body —
    and crucially the sensitive fields (``model`` / ``system_prompt``)
    stay *inside* ``manifest``, never promoted to the envelope. The
    envelope itself carries lightweight metadata (``version``,
    ``content_hash``, ``updated_at``) for the picker; those are not
    leakage.
    """
    resp = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/manifest")
    for row in resp.json():
        if row.get("manifest") is None:
            assert row["manifest"] is None
            # The bytes that would leak — model + system prompt — must never
            # appear at the envelope level. They only live inside `manifest`.
            assert "model" not in row
            assert "system_prompt" not in row


def test_register_endpoint_accepts_legacy_shape_without_new_fields(
    client: TestClient,
) -> None:
    """A manifest from an older SDK (no model / system_prompt keys) still 201s.

    Backwards-compat guarantee: when the dashboard ships ahead of every
    deployed SDK, older `fortify register` calls must keep working against
    the new platform. The new fields are Optional with `None` defaults, so
    Pydantic validation should accept payloads that omit them entirely.
    """
    payload = {
        "manifest": {
            "name": "legacy_agent",
            "description": "registered by an old SDK",
            "framework": "fortify",
            "tools": [],
        }
    }
    resp = client.post(
        "/v1/agents",
        json=payload,
        headers={"Authorization": "Bearer fake-but-unauthenticated"},
    )
    # The legacy-shape body validates; the request itself fails auth (401)
    # rather than schema validation (422). 422 here would mean we broke
    # backwards compatibility.
    assert resp.status_code != 422, resp.text


# ---------------------------------------------------------------------------
# Phase 6: bearer-only ``GET /v1/agents/{name}`` + ``GET /v1/me/key``
#
# Both routes derive project_id from the bearer token; no project_id in
# the URL. Counterparts of the legacy ``GET /v1/projects/{p}/agents/{n}``
# dual-auth route and the CLI's startup introspection call.
# ---------------------------------------------------------------------------


def _mint_token_for_test(session_factory) -> str:
    """Helper: mint a real biscuit-backed token for the seed project.

    Returns the full ``fty_<env>_<project>_<biscuit>`` envelope so the
    test can drop it into an ``Authorization: Bearer …`` header.
    """
    import asyncio

    from services import mint_dev_token

    async def _mint():
        async with session_factory() as session:
            _row, full = await mint_dev_token(
                session,
                DEFAULT_PROJECT_ID,
                name="phase6-test",
                scopes=["read"],
                env="live",
                signing_key_bytes=main.keystore._private_key_bytes(),
            )
            await session.commit()
            return full

    return asyncio.get_event_loop().run_until_complete(_mint())


def test_bearer_get_agent_resolves_project_from_token(
    client: TestClient, session_factory
) -> None:
    """``GET /v1/agents/default`` with a valid bearer → 200 + AgentRead.

    The seed project has a ``default`` agent (created by
    ``ensure_default_project``); the bearer is freshly minted against
    that same project, so the lookup must succeed.
    """
    token = _mint_token_for_test(session_factory)
    r = client.get(
        "/v1/agents/default",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "default"
    # Project resolution is implicit — confirm by asserting the response
    # carries the agent's policy_yaml (a non-trivial column the legacy
    # path would surface identically).
    assert "policy_yaml" in body and len(body["policy_yaml"]) > 0


def test_bearer_get_agent_rejects_missing_authorization(
    client: TestClient,
) -> None:
    """No ``Authorization`` header → 401, not a 404 or 422.

    The route's auth gate must fire before the path lookup; otherwise
    an unauthenticated caller could probe agent names by status code
    (404 = doesn't exist, 200 = leaks existence).
    """
    r = client.get("/v1/agents/anything")
    assert r.status_code == 401


def test_bearer_get_agent_returns_304_on_matching_etag(
    client: TestClient, session_factory
) -> None:
    """Conditional GET with the bundle's ETag → 304 Not Modified.

    Mirrors the legacy route's behaviour so the SDK's per-run
    refresh logic keeps working when the bundle hasn't changed. We
    PUT first so a signed bundle exists in the test DB — the fixture
    skips the lifespan's ``backfill_bundles`` step so the seed agent
    starts unsigned.
    """
    # Touch the agent so the platform compiles + signs the bundle. Use
    # the cookie-authed PUT path with the X-Dev-User test seam (already
    # enabled by conftest).
    put = client.put(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/default",
        headers={"X-Dev-User": DEFAULT_USER_ID},
        json={"policy_yaml": _trivial_policy_yaml()},
    )
    assert put.status_code == 200, put.text

    token = _mint_token_for_test(session_factory)
    first = client.get(
        "/v1/agents/default",
        headers={"Authorization": f"Bearer {token}"},
    )
    etag = first.headers.get("etag")
    assert etag is not None, first.headers

    second = client.get(
        "/v1/agents/default",
        headers={
            "Authorization": f"Bearer {token}",
            "If-None-Match": etag,
        },
    )
    assert second.status_code == 304


def _trivial_policy_yaml() -> str:
    """A policy that compiles cleanly — enough to trigger bundle signing."""
    return (
        "version: 1\n"
        "name: default\n"
        "rules:\n"
        "  - effect: allow\n"
        "    when:\n"
        "      tool: any\n"
    )


def test_me_key_introspects_token_metadata(
    client: TestClient, session_factory
) -> None:
    """``GET /v1/me/key`` describes the bearer without round-tripping it.

    Returns project_id + env + name + scopes. Doesn't echo the token
    value or any secret material — the bearer authenticates the
    request, the response is metadata only.
    """
    token = _mint_token_for_test(session_factory)
    r = client.get(
        "/v1/me/key",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == DEFAULT_PROJECT_ID
    assert body["env"] == "live"
    assert body["name"] == "phase6-test"
    assert body["scopes"] == ["read"]
    # Defensive: the full envelope must never leak in the body.
    assert token not in r.text


def test_me_key_rejects_missing_bearer(client: TestClient) -> None:
    """No ``Authorization`` header → 401."""
    r = client.get("/v1/me/key")
    assert r.status_code == 401
