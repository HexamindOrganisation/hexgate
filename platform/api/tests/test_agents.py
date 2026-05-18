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

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import main
from main import app
from services import DEFAULT_PROJECT_ID, ensure_default_project


@pytest.fixture
def engine():
    """A fresh in-memory SQLite engine shared across sessions.

    StaticPool keeps every Session bound to the same connection — otherwise
    each :memory: handle gets its own private DB and the tables vanish.
    Split out from ``client`` so tests that need to seed rows directly
    (e.g. registering an AgentVersion) can take both fixtures.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as bootstrap:
        ensure_default_project(bootstrap)
    return engine


@pytest.fixture
def client(engine) -> TestClient:
    """TestClient that routes ``get_session`` through the shared test engine."""

    def override_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[main.get_session] = override_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


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
# GET /agents/manifest — dashboard read path sourced from AgentVersion + Tool
# ---------------------------------------------------------------------------


def _sample_manifest(name: str, *, description: str | None = None) -> dict:
    """Minimal AgentManifest payload for register_manifest in tests."""
    return {
        "name": name,
        "description": description,
        "framework": "fortify",
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


def test_manifest_endpoint_returns_registered_manifest_with_tools(
    client: TestClient, engine
) -> None:
    """After register_manifest, the endpoint surfaces the full manifest."""
    from schemas import AgentManifest
    from services import register_manifest

    with Session(engine) as session:
        manifest = AgentManifest.model_validate(
            _sample_manifest("support_bot", description="customer support")
        )
        register_manifest(session, DEFAULT_PROJECT_ID, manifest)

    resp = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/manifest")
    row = next(r for r in resp.json() if r["name"] == "support_bot")
    assert row["version"] == 1
    assert row["content_hash"]
    assert row["manifest"]["description"] == "customer support"
    assert [t["name"] for t in row["manifest"]["tools"]] == ["echo"]
    assert row["manifest"]["tools"][0]["input_schema"]["required"] == ["msg"]


def test_manifest_endpoint_returns_latest_version(
    client: TestClient, engine
) -> None:
    """When multiple versions exist, only the highest one is returned."""
    from schemas import AgentManifest
    from services import register_manifest

    with Session(engine) as session:
        v1 = AgentManifest.model_validate(_sample_manifest("support_bot"))
        v2 = AgentManifest.model_validate(
            {**_sample_manifest("support_bot"), "description": "v2"}
        )
        register_manifest(session, DEFAULT_PROJECT_ID, v1)
        register_manifest(session, DEFAULT_PROJECT_ID, v2)

    resp = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/manifest")
    row = next(r for r in resp.json() if r["name"] == "support_bot")
    assert row["version"] == 2
    assert row["manifest"]["description"] == "v2"
