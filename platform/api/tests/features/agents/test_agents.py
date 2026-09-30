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

import shutil

import pytest
import pytest_asyncio
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy import text
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.core import keystore as keystore_mod
from hexgate_api.core.ids import new_id
from hexgate_api.main import app
from hexgate_api.models import Agent, AgentVersion, Skill
from hexgate_api.seeds.defaults import ensure_default_project
from hexgate_api.constants import DEFAULT_PROJECT_ID, DEFAULT_USER_ID


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
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as bootstrap:
        await ensure_default_project(bootstrap)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def client(session_factory, tmp_path) -> TestClient:
    """TestClient that routes ``get_session`` through the shared test factory."""
    from hexgate_api.core.keystore import FileKeyStore

    async def override_session():
        async with session_factory() as session:
            yield session

    # Endpoints depend on core.db.get_session; override that shared object.
    from hexgate_api.core.db import get_session

    app.dependency_overrides[get_session] = override_session
    # PUT /agents now compiles + signs the policy bundle, so the endpoint
    # needs an initialised keystore. The fixture doesn't run the app
    # lifespan, so wire up a throwaway temp-dir keystore here and restore
    # the original afterward.
    original_keystore = keystore_mod.keystore
    keystore_mod.keystore = FileKeyStore(base_dir=tmp_path / "keystore")
    keystore_mod.keystore.ensure_keypair()
    try:
        # Bake the dev-user header into every request so M3 Phase 2's
        # require_org_member dependency lets these tests through. The
        # default seed user is a member of support-bot's org.
        yield TestClient(app, headers={"X-Dev-User": DEFAULT_USER_ID})
    finally:
        app.dependency_overrides.clear()
        keystore_mod.keystore = original_keystore


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
    before = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot").json()

    resp = client.put(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot",
        json={"system_md": "Updated prompt."},
    )
    assert resp.status_code == 200
    after = resp.json()
    assert after["system_md"] == "Updated prompt."
    assert after["policy_yaml"] == before["policy_yaml"]
    assert after["agent_yaml"] == before["agent_yaml"]


def test_put_agent_rejects_a_policy_that_does_not_load(client: TestClient) -> None:
    """A broken policy fails the save instead of nulling the bundle behind a 200.

    ``compile_bundle`` degrades any compile failure to "no bundle", so this used
    to save, drop the agent's bundle, and fall back to the pydantic engine with
    nothing on the wire to say so.
    """
    before = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot").json()

    resp = client.put(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot",
        json={
            "policy_yaml": (
                "version: 1\n"
                "roles:\n"
                "  default:\n"
                "    tools:\n"
                "      refund_order:\n"
                "        mode: allow\n"
                "        constraints:\n"
                "          - args.amount ~~ 50\n"
            )
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "nothing was saved" in detail["message"]
    assert detail["errors"][0]["role"] == "default"

    after = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot").json()
    assert after["policy_yaml"] == before["policy_yaml"]


def test_put_agent_rejects_a_mistyped_tool_level_fence(client: TestClient) -> None:
    """``contraints:`` on a tool is a dropped cap, so the save must fail.

    The document- and role-level guards never saw this one — it parsed as an
    ``allow`` with no constraints at all.
    """
    resp = client.put(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot",
        json={
            "policy_yaml": (
                "version: 1\n"
                "roles:\n"
                "  default:\n"
                "    tools:\n"
                "      refund_order:\n"
                "        mode: allow\n"
                "        contraints:\n"
                "          - args.amount <= 500\n"
            )
        },
    )
    assert resp.status_code == 422


def test_put_agent_without_policy_yaml_skips_the_policy_gate(
    client: TestClient,
) -> None:
    """The gate reads ``policy_yaml``; an update that omits it still saves."""
    resp = client.put(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot",
        json={"system_md": "Only the prompt changed."},
    )
    assert resp.status_code == 200


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
    assert resp.json() == {"ok": True, "errors": [], "warnings": []}


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
    assert resp.json() == {"ok": True, "errors": [], "warnings": []}


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


def test_validate_reports_a_non_mapping_document(client: TestClient) -> None:
    """Valid YAML that isn't a mapping has no keys to walk — report it rather
    than raising on the first lookup."""
    resp = client.post(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot/validate",
        json={"policy_yaml": "- refund_order\n- web_search\n"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    [err] = body["errors"]
    assert "must be a YAML mapping" in err["message"]


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
# /validate warnings — the permissive-default lint (D14) in the editor
# ---------------------------------------------------------------------------

_PERMISSIVE_DEFAULT_YAML = (
    "version: 1\n"
    "roles:\n"
    "  default:\n"
    "    tools:\n"
    "      refund_order: { mode: allow }\n"
    "  support:\n"
    "    tools:\n"
    "      read_ticket: { mode: allow }\n"
)


def test_validate_warns_when_default_grants_what_no_named_role_grants(
    client: TestClient,
) -> None:
    """``default`` catches every undefined role name, so a grant only it
    carries is reachable by any caller — a warning, never a save-blocking error."""
    resp = client.post(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot/validate",
        json={"policy_yaml": _PERMISSIVE_DEFAULT_YAML},
    )
    assert resp.status_code == 200
    body = resp.json()
    # The document is valid YAML and valid policy — ok stays True.
    assert body["ok"] is True
    assert body["errors"] == []
    [warning] = body["warnings"]
    assert "permissive-default" in warning["message"]
    assert "refund_order" in warning["message"]
    # The locus is a tool, not a role. Both render as a bare name in the same
    # slot, so putting it in ``role`` would invent a role called refund_order.
    assert warning["tool"] == "refund_order"
    assert warning["role"] is None


def test_validate_warning_does_not_block_ok(client: TestClient) -> None:
    """``ok = not errors``, so a lint can't block the dashboard's save path."""
    resp = client.post(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot/validate",
        json={"policy_yaml": _PERMISSIVE_DEFAULT_YAML},
    )
    body = resp.json()
    assert body["warnings"] and body["ok"] is True


def test_validate_least_privilege_default_produces_no_warning(
    client: TestClient,
) -> None:
    """Every ``default`` grant is also granted by a named role → nothing to flag."""
    resp = client.post(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot/validate",
        json={
            "policy_yaml": (
                "version: 1\n"
                "roles:\n"
                "  default:\n"
                "    tools:\n"
                "      read_ticket: { mode: allow }\n"
                "  support:\n"
                "    tools:\n"
                "      read_ticket: { mode: allow }\n"
                "      refund_order: { mode: allow }\n"
            )
        },
    )
    body = resp.json()
    assert body["ok"] is True
    assert body["warnings"] == []


def test_validate_single_role_policy_is_never_warned_about(
    client: TestClient,
) -> None:
    """A flat policy.yaml *is* the ``default`` role, so warning on it would fire
    on every single-role agent."""
    resp = client.post(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/default/validate",
        json={
            "policy_yaml": (
                "version: 1\n"
                "default_policy: { mode: allow }\n"
                "tools:\n"
                "  web_search: { mode: allow }\n"
            )
        },
    )
    body = resp.json()
    assert body["ok"] is True
    assert body["warnings"] == []


def test_validate_reports_no_warnings_when_the_document_has_errors(
    client: TestClient,
) -> None:
    """The lint needs a loaded PolicySet, which a broken document can't produce.
    Errors alone are reported rather than a confusing partial lint."""
    resp = client.post(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot/validate",
        json={"policy_yaml": "tools: [bad: unclosed\n"},
    )
    body = resp.json()
    assert body["ok"] is False
    assert body["warnings"] == []


def test_validate_rejects_an_unknown_sibling_of_roles(client: TestClient) -> None:
    """A mistyped file-level key is a load error, so validate must not pass it.

    The per-role pass only walks what sits under ``roles:``. Answering ``ok``
    here sent the operator on to save a document the compiler drops the bundle
    for, leaving the agent unable to load its policy at all.
    """
    resp = client.post(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot/validate",
        json={
            "policy_yaml": (
                "contraints:\n"
                "  - run.tool_calls < 5\n"
                "roles:\n"
                "  default:\n"
                "    default_policy: { mode: allow }\n"
            )
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "contraints" in body["errors"][0]["message"]


def test_validate_rejects_a_malformed_file_level_constraints_block(
    client: TestClient,
) -> None:
    """The file-level fence is grammar-checked, not just the per-role ones."""
    resp = client.post(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot/validate",
        json={
            "policy_yaml": (
                "constraints: run.tool_calls < 5\n"
                "roles:\n"
                "  default:\n"
                "    default_policy: { mode: allow }\n"
            )
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "constraints" in body["errors"][0]["message"]


def test_validate_accepts_a_file_level_constraints_block(client: TestClient) -> None:
    """The hoisted fence is legal — the guards above must not reject the shape
    the SDK now supports."""
    resp = client.post(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot/validate",
        json={
            "policy_yaml": (
                "version: 1\n"
                "constraints:\n"
                "  - run.tool_calls < 20\n"
                "roles:\n"
                "  default:\n"
                "    tools:\n"
                "      read_ticket: { mode: allow }\n"
            )
        },
    )
    body = resp.json()
    assert body["ok"] is True, body
    assert body["errors"] == []


def test_validate_unresolvable_inheritance_does_not_500(client: TestClient) -> None:
    """Roles that each parse but whose inheritance can't resolve reach the
    PolicySet build and must be reported there, never crash.

    The per-role pass can't see a link failure, so this used to answer ``ok``
    for a document the compiler and the SDK both reject."""
    resp = client.post(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot/validate",
        json={
            "policy_yaml": (
                "version: 1\n"
                "roles:\n"
                "  default:\n"
                "    tools:\n"
                "      read_ticket: { mode: allow }\n"
                # Well-formed per role; fails only at PolicySet link time.
                "  support:\n"
                "    inherits: [does_not_exist]\n"
                "    tools:\n"
                "      refund_order: { mode: allow }\n"
            )
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "does_not_exist" in body["errors"][0]["message"]
    assert body["warnings"] == []


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
    skills: list[dict] | None = None,
) -> dict:
    """Minimal AgentManifest payload for register_manifest in tests.

    ``skills`` stays out of the payload entirely when None — the shape every
    framework without a skill concept sends.
    """
    return {
        "name": name,
        "description": description,
        "framework": "hexgate",
        "model": model,
        "system_prompt": system_prompt,
        **({"skills": skills} if skills is not None else {}),
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
    from hexgate_api.schemas import AgentManifest
    from hexgate_api.features.agents.service import register_manifest

    async with session_factory() as session:
        manifest = AgentManifest.model_validate(
            _sample_manifest("support_bot", description="customer support")
        )
        # ``sign=`` is required from Phase 7 step 1 onward — wire the
        # test keystore through so first-time agents get a real signed
        # bundle (when opa's available) and the starter policy_yaml.
        await register_manifest(
            session, DEFAULT_PROJECT_ID, manifest, sign=keystore_mod.keystore.sign
        )

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
    from hexgate_api.schemas import AgentManifest
    from hexgate_api.features.agents.service import register_manifest

    async with session_factory() as session:
        v1 = AgentManifest.model_validate(_sample_manifest("support_bot"))
        v2 = AgentManifest.model_validate(
            {**_sample_manifest("support_bot"), "description": "v2"}
        )
        # ``sign=`` required from Phase 7 step 1 onward.
        await register_manifest(
            session, DEFAULT_PROJECT_ID, v1, sign=keystore_mod.keystore.sign
        )
        await register_manifest(
            session, DEFAULT_PROJECT_ID, v2, sign=keystore_mod.keystore.sign
        )

    resp = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/manifest")
    row = next(r for r in resp.json() if r["name"] == "support_bot")
    assert row["version"] == 2
    assert row["manifest"]["description"] == "v2"


async def test_manifest_endpoint_round_trips_model_and_system_prompt(
    client: TestClient, session_factory
) -> None:
    """The new manifest fields survive register → read."""
    from hexgate_api.schemas import AgentManifest
    from hexgate_api.features.agents.service import register_manifest

    async with session_factory() as session:
        manifest = AgentManifest.model_validate(
            _sample_manifest(
                "support_bot",
                model="gpt-4o-mini",
                system_prompt="be helpful",
            )
        )
        # ``sign=`` is required from Phase 7 step 1 onward — wire the
        # test keystore through so first-time agents get a real signed
        # bundle (when opa's available) and the starter policy_yaml.
        await register_manifest(
            session, DEFAULT_PROJECT_ID, manifest, sign=keystore_mod.keystore.sign
        )

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
    client: TestClient, session_factory
) -> None:
    """A manifest from an older SDK (no model / system_prompt keys) still 201s.

    Backwards-compat guarantee: when the dashboard ships ahead of every
    deployed SDK, older `hexgate register` calls must keep working against
    the new platform. The new fields are Optional with `None` defaults, so
    Pydantic validation should accept payloads that omit them entirely.
    """
    token = _mint_owned_token(session_factory, owner_user_id=DEFAULT_USER_ID)
    payload = {
        "manifest": {
            "name": "legacy_agent",
            "description": "registered by an old SDK",
            "framework": "hexgate",
            "tools": [],
        }
    }
    resp = client.post(
        "/v1/agents", json=payload, headers={"Authorization": f"Bearer {token}"}
    )
    # Authenticated on purpose. This used to send an unauthenticated bearer and
    # assert ``!= 422`` on the belief that the body validates first and only
    # auth fails — it does not. FastAPI resolves dependencies BEFORE validating
    # the body, so that request 401s with the schema never reached, and the
    # assertion held even for a manifest missing every required field.
    assert resp.status_code == 201, resp.text


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

    from hexgate_api.features.tokens.service import mint_api_key

    async def _mint():
        async with session_factory() as session:
            _row, full = await mint_api_key(
                session,
                DEFAULT_PROJECT_ID,
                name="phase6-test",
                scopes=["read"],
                env="live",
                signing_key_bytes=keystore_mod.keystore._private_key_bytes(),
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
        "default_policy:\n"
        "  mode: deny\n"
        "tools:\n"
        "  read_ticket:\n"
        "    mode: allow\n"
    )


def test_me_key_introspects_token_metadata(client: TestClient, session_factory) -> None:
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


# ---------------------------------------------------------------------------
# Phase 7 step 1: register-time default policy + signed bundle
#
# A brand-new agent registered via POST /v1/agents lands with:
#   - a starter role-aware policy_yaml (4 roles inc. mixin)
#   - a signed WASM bundle (when opa is on PATH)
# Re-registers of an existing agent leave the operator's policy_yaml
# alone — only the AgentVersion snapshot history grows.
# ---------------------------------------------------------------------------


_OPA_AVAILABLE = shutil.which("opa") is not None
_needs_opa = pytest.mark.skipif(not _OPA_AVAILABLE, reason="opa not on PATH")


def _register_payload(name: str, tools: list[str]) -> dict:
    """Helper: build the JSON body POST /v1/agents expects."""
    return {
        "manifest": {
            "name": name,
            "description": f"test agent {name}",
            "framework": "langchain",
            "tools": [
                {
                    "name": t,
                    "description": None,
                    "input_schema": {"properties": {}, "required": []},
                }
                for t in tools
            ],
        }
    }


def _load_agent_row(session_factory, project_id: str, name: str):
    """Read an Agent row back out of the test DB via the same session
    factory the route handlers used. Reaches around the API on purpose
    — we want to see what actually got written, not what got returned."""
    import asyncio

    from hexgate_api.models import Agent
    from sqlmodel import select

    async def _fetch():
        async with session_factory() as session:
            return (
                await session.exec(
                    select(Agent).where(
                        Agent.project_id == project_id,
                        Agent.name == name,
                    )
                )
            ).first()

    return asyncio.get_event_loop().run_until_complete(_fetch())


def test_register_first_time_generates_starter_policy_yaml(
    client: TestClient, session_factory
) -> None:
    """First POST /v1/agents for a fresh name populates policy_yaml with
    the four-role starter — not the legacy ``""`` placeholder.

    Doesn't require opa: signing is best-effort, the policy_yaml is
    populated unconditionally.
    """
    token = _mint_token_for_test(session_factory)
    r = client.post(
        "/v1/agents",
        json=_register_payload("phase7_fresh", ["web_search", "write_file", "bash"]),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text

    agent = _load_agent_row(session_factory, DEFAULT_PROJECT_ID, "phase7_fresh")
    assert agent is not None
    # Must not be the legacy empty default.
    assert agent.policy_yaml != ""
    # Round-trips through the SDK's loader — same code path the
    # SDK runs at every chat turn.
    from hexgate.security import load_policy_set_from_dict

    policy_set = load_policy_set_from_dict(yaml.safe_load(agent.policy_yaml))
    assert sorted(policy_set.roles) == ["admin", "default", "member"]
    # Each bucket landed in the right role:
    assert policy_set.policy_for("admin").tools["web_search"].mode == "allow"
    assert policy_set.policy_for("admin").tools["write_file"].mode == "allow"
    assert (
        policy_set.policy_for("member").tools["write_file"].mode == "approval_required"
    )
    assert policy_set.policy_for("admin").tools["bash"].mode == "approval_required"


@_needs_opa
def test_register_first_time_compiles_signed_bundle(
    client: TestClient, session_factory
) -> None:
    """When opa is available, the new agent ships a signed WASM bundle
    so ``hexgate serve`` runs against the real enforcement engine from
    the first request (not the pydantic fallback)."""
    token = _mint_token_for_test(session_factory)
    r = client.post(
        "/v1/agents",
        json=_register_payload("phase7_signed", ["read_file", "write_file"]),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201

    agent = _load_agent_row(session_factory, DEFAULT_PROJECT_ID, "phase7_signed")
    assert agent is not None
    # All three bundle columns populated together.
    assert agent.compiled_wasm is not None
    assert agent.bundle_manifest is not None
    assert agent.bundle_signature is not None


def test_register_preserves_operator_edited_policy_on_subsequent_register(
    client: TestClient, session_factory
) -> None:
    """Once the operator edits ``policy_yaml`` (via the dashboard's PUT
    route), re-registering the same agent must NOT clobber those
    edits — only the manifest snapshot grows. Policy belongs to the
    operator after first registration.
    """
    token = _mint_token_for_test(session_factory)
    # First register — establishes the agent + auto-generated policy.
    r1 = client.post(
        "/v1/agents",
        json=_register_payload("phase7_edited", ["read_file"]),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r1.status_code == 201

    # Operator edits the policy via the cookie-auth PUT route.
    edited = "version: 1\nroles:\n  default:\n    default_policy:\n      mode: deny\n"
    put = client.put(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/phase7_edited",
        headers={"X-Dev-User": DEFAULT_USER_ID},
        json={"policy_yaml": edited},
    )
    assert put.status_code == 200, put.text

    # Re-register with the same manifest content → 200 (no-op) or 201
    # with new version. Either way, agent.policy_yaml stays as the
    # operator's edited version.
    r2 = client.post(
        "/v1/agents",
        json=_register_payload("phase7_edited", ["read_file"]),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code in (200, 201)

    agent = _load_agent_row(session_factory, DEFAULT_PROJECT_ID, "phase7_edited")
    assert agent is not None
    assert agent.policy_yaml == edited


def test_register_with_different_tools_leaves_existing_policy_alone(
    client: TestClient, session_factory
) -> None:
    """An operator who registers an agent, edits the policy, then adds
    a new tool to the manifest and re-registers should NOT see the
    policy regenerated. The new tool will be unmentioned in the
    policy — that's correct, the operator decides how to handle it.
    """
    token = _mint_token_for_test(session_factory)

    # First register with one tool.
    r1 = client.post(
        "/v1/agents",
        json=_register_payload("phase7_toolchurn", ["read_file"]),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r1.status_code == 201
    agent_after_first = _load_agent_row(
        session_factory, DEFAULT_PROJECT_ID, "phase7_toolchurn"
    )
    starter = agent_after_first.policy_yaml
    assert "read_file" in starter

    # Re-register with a manifest that ADDS a new tool. Triggers a
    # new AgentVersion (different content_hash) but must not touch
    # the Agent.policy_yaml.
    r2 = client.post(
        "/v1/agents",
        json=_register_payload("phase7_toolchurn", ["read_file", "write_file"]),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status_code == 201  # New version → 201, not 200

    agent_after_second = _load_agent_row(
        session_factory, DEFAULT_PROJECT_ID, "phase7_toolchurn"
    )
    # Policy unchanged — the new tool name doesn't appear because the
    # auto-generation only fires on first create.
    assert agent_after_second.policy_yaml == starter
    assert "write_file" not in agent_after_second.policy_yaml


# ---------------------------------------------------------------------------
# require_project — the SDK bearer-auth gate
#
# Pre-fix, the gate only ran ``find_token_by_secret`` (a plain
# secret-string lookup) — a forged biscuit whose payload happened to
# match a stored secret would pass. The fix routes through
# ``_validate_sdk_token`` first, which adds the signature gate
# ws_require_project already had.
# ---------------------------------------------------------------------------


def test_require_project_rejects_token_with_bad_signature(
    client: TestClient, session_factory
) -> None:
    """A bearer whose envelope decodes but signature doesn't chain to
    the platform's root key → 401, even when the surrounding token
    string is byte-identical to a stored secret.

    Synthesised by minting a valid token, then tampering the last few
    chars of the biscuit b64 payload before sending it. The DB still
    has the original (untampered) secret, so the revocation lookup
    would silently accept the tampered string under the pre-fix
    behaviour. The signature gate now catches it first.
    """
    token = _mint_token_for_test(session_factory)
    # Flip the trailing 4 chars of the biscuit_b64 payload. The
    # envelope is ``fty_<env>_<project>_<biscuit>`` — keep the prefix
    # intact so parse_envelope succeeds, mutate the signed payload so
    # verify_token fails.
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    assert tampered != token  # sanity

    r = client.post(
        "/v1/agents",
        json={
            "manifest": {
                "name": "tampered_test",
                "description": None,
                "framework": "langchain",
                "tools": [],
            }
        },
        headers={"Authorization": f"Bearer {tampered}"},
    )
    assert r.status_code == 401, r.text
    assert (
        "signature" in r.json()["detail"].lower()
        or "malformed" in r.json()["detail"].lower()
    )


def test_require_project_accepts_valid_signed_token(
    client: TestClient, session_factory
) -> None:
    """Happy-path sanity check — a freshly-minted token still works
    after the signature gate is added. Guards against the regression
    where adding _validate_sdk_token would over-reject."""
    token = _mint_token_for_test(session_factory)
    r = client.post(
        "/v1/agents",
        json={
            "manifest": {
                "name": "sig_gate_happy_path",
                "description": None,
                "framework": "langchain",
                "tools": [],
            }
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Actor trail (issue #160)
#
# Two writers land here: the cookie-authed PUT (a human) and the SDK's
# bearer-authed register (a token, bridged back to a person through
# ``ApiKey.owner_user_id``).
# ---------------------------------------------------------------------------


def _mint_owned_token(session_factory, *, owner_user_id: str | None) -> str:
    """Mint a key for the seed project with a given owner (or none)."""
    import asyncio

    from hexgate_api.features.tokens.service import mint_api_key

    async def _mint() -> str:
        async with session_factory() as session:
            _row, full = await mint_api_key(
                session,
                DEFAULT_PROJECT_ID,
                name=f"actor-{owner_user_id or 'system'}",
                scopes=["read"],
                env="live",
                signing_key_bytes=keystore_mod.keystore._private_key_bytes(),
                created_by_user_id=owner_user_id,
                owner_user_id=owner_user_id,
            )
            await session.commit()
            return full

    return asyncio.get_event_loop().run_until_complete(_mint())


def _read_agent(session_factory, *, project_id: str, name: str) -> Agent:
    import asyncio

    from hexgate_api.features.agents.service import get_agent

    async def _get() -> Agent:
        async with session_factory() as session:
            row = await get_agent(session, project_id, name)
            assert row is not None
            return row

    return asyncio.get_event_loop().run_until_complete(_get())


def _read_latest_version(session_factory, agent_id: str) -> AgentVersion:
    import asyncio

    async def _get() -> AgentVersion:
        async with session_factory() as session:
            rows = (
                await session.exec(
                    select(AgentVersion)
                    .where(AgentVersion.agent_id == agent_id)
                    .order_by(AgentVersion.version.desc())
                )
            ).all()
            assert rows
            return rows[0]

    return asyncio.get_event_loop().run_until_complete(_get())


def _manifest(name: str) -> dict:
    return {
        "manifest": {
            "name": name,
            "framework": "hexgate",
            "tools": [
                {
                    "name": "ping",
                    "input_schema": {
                        "properties": {"q": {"title": "Q", "type": "string"}},
                        "required": ["q"],
                    },
                }
            ],
        }
    }


def test_put_agent_stamps_the_editing_user(client: TestClient, session_factory) -> None:
    before = _read_agent(session_factory, project_id=DEFAULT_PROJECT_ID, name="default")
    assert before.updated_by_user_id is None

    r = client.put(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/default",
        json={"system_md": "edited by a human"},
        headers={"X-Dev-User": DEFAULT_USER_ID},
    )
    assert r.status_code == 200, r.text

    after = _read_agent(session_factory, project_id=DEFAULT_PROJECT_ID, name="default")
    assert after.updated_by_user_id == DEFAULT_USER_ID
    assert after.updated_at > before.updated_at


def test_seeded_agents_carry_no_actor(session_factory) -> None:
    """``ensure_seeded_agents`` is a system write — NULL, not a sentinel."""
    row = _read_agent(session_factory, project_id=DEFAULT_PROJECT_ID, name="default")
    assert row.created_by_user_id is None


def test_register_agent_stamps_the_keys_owner(
    client: TestClient, session_factory
) -> None:
    """The bearer path: ``ApiKey.owner_user_id`` is the only bridge from a
    token back to a person."""
    token = _mint_owned_token(session_factory, owner_user_id=DEFAULT_USER_ID)

    r = client.post(
        "/v1/agents",
        json=_manifest("owned_agent"),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text

    agent = _read_agent(
        session_factory, project_id=DEFAULT_PROJECT_ID, name="owned_agent"
    )
    assert agent.created_by_user_id == DEFAULT_USER_ID
    version = _read_latest_version(session_factory, agent.id)
    assert version.created_by_user_id == DEFAULT_USER_ID


def test_register_agent_with_an_ownerless_key_stamps_null(
    client: TestClient, session_factory
) -> None:
    """A key with no owner still writes; it records NULL rather than a guess."""
    token = _mint_owned_token(session_factory, owner_user_id=None)

    r = client.post(
        "/v1/agents",
        json=_manifest("unowned_agent"),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text

    agent = _read_agent(
        session_factory, project_id=DEFAULT_PROJECT_ID, name="unowned_agent"
    )
    assert agent.created_by_user_id is None
    assert _read_latest_version(session_factory, agent.id).created_by_user_id is None


def test_re_register_does_not_rewrite_the_original_creator(
    client: TestClient, session_factory
) -> None:
    """An agent's creator is whoever first registered it."""
    first = _mint_owned_token(session_factory, owner_user_id=DEFAULT_USER_ID)
    client.post(
        "/v1/agents",
        json=_manifest("stable_creator"),
        headers={"Authorization": f"Bearer {first}"},
    )
    second = _mint_owned_token(session_factory, owner_user_id=None)

    # Same name, different manifest → a new version under the same Agent.
    payload = _manifest("stable_creator")
    payload["manifest"]["description"] = "changed"
    r = client.post(
        "/v1/agents", json=payload, headers={"Authorization": f"Bearer {second}"}
    )
    assert r.status_code in (200, 201), r.text

    agent = _read_agent(
        session_factory, project_id=DEFAULT_PROJECT_ID, name="stable_creator"
    )
    assert agent.created_by_user_id == DEFAULT_USER_ID


async def test_register_stores_subagents_and_preserves_hash_continuity(
    client: TestClient, session_factory
) -> None:
    """subagents round-trip into the stored manifest; an agent without them hashes
    as before (dedup no-op), and adding them yields a new content_hash + version."""
    from hexgate_api.schemas import AgentManifest
    from hexgate_api.features.agents.service import register_manifest

    async with session_factory() as session:
        base = _sample_manifest("support_bot")
        m1 = AgentManifest.model_validate(base)
        v1, created1 = await register_manifest(
            session, DEFAULT_PROJECT_ID, m1, sign=keystore_mod.keystore.sign
        )
        # Re-register the identical (sub-agent-less) manifest → dedup no-op, same hash.
        v1b, created1b = await register_manifest(
            session, DEFAULT_PROJECT_ID, m1, sign=keystore_mod.keystore.sign
        )
        assert created1 is True and created1b is False
        assert v1b.content_hash == v1.content_hash
        # Stored as None (NOT []): a sub-agent-less manifest must carry no subagents key
        # so exclude_none drops it and the hash is unchanged. `is None` (not `in (None,
        # [])`) so a schema default of `[]` — which would remint a version for every
        # already-registered agent — fails here.
        assert (v1.manifest or {}).get("subagents") is None

        # Guard the continuity contract itself: an explicit `[]` is NOT excluded by
        # exclude_none, so it hashes differently from the None (field-absent) manifest —
        # i.e. defaulting the field to `[]` would break dedup for every existing agent.
        m_empty = AgentManifest.model_validate({**base, "subagents": []})
        v_empty, _ = await register_manifest(
            session, DEFAULT_PROJECT_ID, m_empty, sign=keystore_mod.keystore.sign
        )
        assert v_empty.content_hash != v1.content_hash

        # Now register with a sub-agent edge → different hash, new version, stored.
        m2 = AgentManifest.model_validate(
            {**base, "subagents": [{"name": "billing_bot", "via": "tool"}]}
        )
        v2, created2 = await register_manifest(
            session, DEFAULT_PROJECT_ID, m2, sign=keystore_mod.keystore.sign
        )
        assert created2 is True
        assert v2.content_hash != v1.content_hash
        assert v2.manifest["subagents"] == [{"name": "billing_bot", "via": "tool"}]

    # And the read view surfaces the sub-agent edge for the dashboard.
    resp = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/manifest")
    row = next(r for r in resp.json() if r["name"] == "support_bot")
    assert row["manifest"]["subagents"] == [{"name": "billing_bot", "via": "tool"}]


# ---------------------------------------------------------------------------
# M2 — skills carried on the manifest
#
# The platform mirrors the SDK's three shapes and writes one ``skill`` row per
# definition per version, inside the same transaction as the tools. It caps
# nothing and dedupes nothing: the SDK truncates before it sends, and the
# unique constraint is the backstop.
# ---------------------------------------------------------------------------


def _skill_payload(name: str, **overrides: object) -> dict:
    """A fully populated SkillDefinition payload, field-by-field overridable."""
    return {
        "name": name,
        "description": f"what {name} does",
        "source": ".claude/skills",
        "resources": {
            "references": ["reference.md"],
            "assets": ["logo.png"],
            "scripts": ["run.sh"],
        },
        "allowed_tools": ["Read", "Grep"],
        "additional_tools": ["issue_refund"],
        "content_hash": "sha256-of-the-body",
        **overrides,
    }


async def _skills_of(session_factory, agent_version_id: str) -> list[Skill]:
    async with session_factory() as session:
        rows = await session.exec(
            select(Skill).where(Skill.agent_version_id == agent_version_id)
        )
        return sorted(rows.all(), key=lambda row: row.name)


def _skills_of_sync(session_factory, agent_version_id: str) -> list[Skill]:
    """``_skills_of`` for the sync (TestClient + minted token) tests."""
    import asyncio

    return asyncio.get_event_loop().run_until_complete(
        _skills_of(session_factory, agent_version_id)
    )


async def _register(session_factory, payload: dict):
    from hexgate_api.schemas import AgentManifest
    from hexgate_api.features.agents.service import register_manifest

    async with session_factory() as session:
        return await register_manifest(
            session,
            DEFAULT_PROJECT_ID,
            AgentManifest.model_validate(payload),
            sign=keystore_mod.keystore.sign,
        )


async def test_register_manifest_persists_skill_rows(session_factory) -> None:
    """One row per definition, every field round-tripping through the JSON."""
    version, created = await _register(
        session_factory,
        _sample_manifest(
            "skilled_bot",
            skills=[_skill_payload("refunds"), _skill_payload("escalation")],
        ),
    )
    assert created

    rows = await _skills_of(session_factory, version.id)
    assert [row.name for row in rows] == ["escalation", "refunds"]

    refunds = rows[1]
    assert refunds.description == "what refunds does"
    assert refunds.source == ".claude/skills"
    assert refunds.resources == {
        "references": ["reference.md"],
        "assets": ["logo.png"],
        "scripts": ["run.sh"],
    }
    assert refunds.allowed_tools == ["Read", "Grep"]
    assert refunds.additional_tools == ["issue_refund"]
    assert refunds.content_hash == "sha256-of-the-body"
    assert refunds.id.startswith("skl_")


async def test_register_manifest_without_skills_writes_no_skill_rows(
    session_factory,
) -> None:
    """``skills=None`` — every framework with no skill concept — is a no-op."""
    version, created = await _register(
        session_factory, _sample_manifest("skill_less_bot")
    )
    assert created
    assert await _skills_of(session_factory, version.id) == []


def test_register_endpoint_accepts_manifest_without_skills_key(
    client: TestClient, session_factory
) -> None:
    """A body omitting ``skills`` entirely registers — the compat guarantee.

    Authenticated on purpose. FastAPI resolves dependencies before it
    validates the body, so an unauthenticated request 401s without the schema
    ever being reached: asserting ``!= 422`` there would pass even against a
    manifest missing every required field.
    """
    token = _mint_owned_token(session_factory, owner_user_id=DEFAULT_USER_ID)
    payload = {
        "manifest": {
            "name": "no_skills_key",
            "framework": "hexgate",
            "tools": [],
        }
    }
    resp = client.post(
        "/v1/agents", json=payload, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 201, resp.text

    version_id = resp.json()["agent_version_id"]
    assert _skills_of_sync(session_factory, version_id) == []


def test_manifest_hash_is_unchanged_by_the_new_field() -> None:
    """The G2 guard: adding ``skills`` mints no version for agents without any.

    ``compute_manifest_hash`` excludes None, so a pre-skills payload and the
    same payload carrying an explicit ``skills: null`` hash identically — an
    old SDK re-registering against the new platform matches its existing
    version rather than duplicating it.
    """
    from hexgate_api.schemas import AgentManifest
    from hexgate_api.features.agents.service import compute_manifest_hash

    payload = _sample_manifest("hash_stability")
    before = compute_manifest_hash(AgentManifest.model_validate(payload))
    after = compute_manifest_hash(
        AgentManifest.model_validate({**payload, "skills": None})
    )
    assert before == after


async def test_register_manifest_preserves_resources_none_vs_empty(
    session_factory,
) -> None:
    """NULL and ``{}`` mean different things and must read back distinct.

    NULL: the framework does not enumerate L3 contents at all. ``{}``: it does,
    and this skill ships none. M6 renders the two differently.
    """
    version, _ = await _register(
        session_factory,
        _sample_manifest(
            "resource_bot",
            skills=[
                _skill_payload("not_enumerated", resources=None),
                _skill_payload("enumerated_empty", resources={}),
            ],
        ),
    )

    enumerated, not_enumerated = await _skills_of(session_factory, version.id)
    assert not_enumerated.resources is None
    assert enumerated.resources == {"references": [], "assets": [], "scripts": []}

    # And distinct in SQL, not just after the ORM decodes them. Without
    # ``none_as_null`` on the column, None persists as the JSON scalar
    # ``'null'`` — the ORM still reads back None, so the assertions above pass
    # while ``IS NULL`` silently matches nothing.
    async with session_factory() as session:
        rows = dict(
            (
                await session.exec(
                    text(
                        "SELECT name, resources IS NULL FROM skill "
                        "WHERE agent_version_id = :v"
                    ).bindparams(v=version.id)
                )
            ).all()
        )
    assert rows == {"not_enumerated": 1, "enumerated_empty": 0}


async def test_duplicate_skill_names_in_one_version_are_rejected(
    session_factory,
) -> None:
    """The service rejects duplicates before the DB has to.

    The SDK collapses same-name skills before sending, so this only fires for
    a caller that bypasses it. Pre-checked rather than left to UNIQUE because
    nothing registers an exception handler — an IntegrityError escaping the
    route would be a 500 with no usable message.
    """
    from hexgate_api.features.agents.service import DuplicateSkillNameError

    with pytest.raises(DuplicateSkillNameError):
        await _register(
            session_factory,
            _sample_manifest(
                "duplicate_bot",
                skills=[_skill_payload("twice"), _skill_payload("twice")],
            ),
        )


def test_duplicate_skill_names_surface_as_409(
    client: TestClient, session_factory
) -> None:
    """What the caller actually sees: a 409, not the default 500."""
    token = _mint_owned_token(session_factory, owner_user_id=DEFAULT_USER_ID)
    payload = {
        "manifest": _sample_manifest(
            "duplicate_over_http",
            skills=[_skill_payload("twice"), _skill_payload("twice")],
        )
    }
    resp = client.post(
        "/v1/agents", json=payload, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 409, resp.text
    assert "twice" in resp.json()["detail"]


async def test_unique_constraint_backstops_duplicate_skill_names(
    session_factory,
) -> None:
    """The DB guard behind the pre-check, asserted directly.

    The service can only refuse what it sees in one manifest; the constraint
    is what stops two rows an agent could never tell apart from ever existing.
    """
    from sqlalchemy.exc import IntegrityError

    async with session_factory() as session:
        for _ in range(2):
            session.add(
                Skill(
                    id=new_id(Skill),
                    agent_version_id="agv_backstop",
                    name="twice",
                    allowed_tools=[],
                    additional_tools=[],
                )
            )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_agent_manifest_view_surfaces_skills(
    client: TestClient, session_factory
) -> None:
    """The read path M6 consumes: skills come back inside ``manifest``."""
    await _register(
        session_factory,
        _sample_manifest(
            "support_bot",
            skills=[_skill_payload("refunds", resources=None)],
        ),
    )

    resp = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/manifest")
    row = next(r for r in resp.json() if r["name"] == "support_bot")
    (skill,) = row["manifest"]["skills"]
    assert skill["name"] == "refunds"
    assert skill["resources"] is None
    assert skill["allowed_tools"] == ["Read", "Grep"]
    assert skill["additional_tools"] == ["issue_refund"]


async def test_re_register_with_new_skills_creates_a_new_version(
    session_factory,
) -> None:
    """The flip side of the hash guard: a real skill change is a real version."""
    first, _ = await _register(session_factory, _sample_manifest("evolving_bot"))
    second, created = await _register(
        session_factory,
        _sample_manifest("evolving_bot", skills=[_skill_payload("refunds")]),
    )

    assert created
    assert second.version == first.version + 1
    assert second.content_hash != first.content_hash
    assert await _skills_of(session_factory, first.id) == []
    assert [row.name for row in await _skills_of(session_factory, second.id)] == [
        "refunds"
    ]


async def test_register_first_time_generates_skills_in_the_starter_policy(
    session_factory,
) -> None:
    """The generator is reached from ``register_manifest``, not just in isolation."""
    await _register(
        session_factory,
        _sample_manifest(
            "starter_skills_bot",
            skills=[
                _skill_payload("runbook", resources=None, additional_tools=[]),
                _skill_payload("refunds"),
            ],
        ),
    )

    async with session_factory() as session:
        agent = (
            await session.exec(
                select(Agent).where(
                    Agent.project_id == DEFAULT_PROJECT_ID,
                    Agent.name == "starter_skills_bot",
                )
            )
        ).one()

    roles = yaml.safe_load(agent.policy_yaml)["roles"]
    assert roles["read_only"]["skills"] == {"runbook": {"mode": "allow"}}
    assert roles["member"]["skills"] == {"refunds": {"mode": "approval_required"}}
    assert roles["admin"]["skills"] == {"refunds": {"mode": "allow"}}
