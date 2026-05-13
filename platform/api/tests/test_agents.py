"""Tests for the role-aware agent shape on the platform API.

The phase 4b seed adds a ``support_bot`` agent with a three-role policy
bundle (``read_only`` mixin, plus ``support`` and ``billing`` concrete
roles). These tests make sure the storage round-trips, the GET endpoint
returns the ``roles`` map verbatim, and PUT can update it without
clobbering the legacy single-policy field.
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
def client() -> TestClient:
    """A fresh in-memory DB + TestClient, seeded with the default project.

    Uses a StaticPool so every Session shares the same SQLite connection —
    otherwise each :memory: handle gets its own private DB and the tables
    we created in the fixture vanish from the endpoint's session view.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    def override_session():
        with Session(engine) as session:
            yield session

    # Endpoints use the local main.get_session, so we override that one.
    app.dependency_overrides[main.get_session] = override_session
    with Session(engine) as bootstrap:
        ensure_default_project(bootstrap)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_get_support_bot_returns_roles_map(client: TestClient) -> None:
    """The seed plants three roles on support_bot; GET surfaces them."""
    resp = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["roles"]) == {"read_only", "default", "support", "billing"}
    # Each role's value parses as a YAML policy.
    for role_name, yaml_text in body["roles"].items():
        parsed = yaml.safe_load(yaml_text)
        assert isinstance(parsed, dict), role_name
        assert parsed.get("version") == 1


def test_get_default_agent_returns_empty_roles(client: TestClient) -> None:
    """The legacy seed agents stay single-policy — ``roles`` empty dict."""
    resp = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/default")
    assert resp.status_code == 200
    assert resp.json()["roles"] == {}


def test_put_agent_updates_roles_map(client: TestClient) -> None:
    """PUT with a fresh ``roles`` dict overwrites the stored map."""
    resp = client.put(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/default",
        json={
            "roles": {
                "default": "version: 1\ntools:\n  refund_order:\n    mode: allow\n"
            }
        },
    )
    assert resp.status_code == 200
    assert resp.json()["roles"].keys() == {"default"}

    # GET picks up the update.
    resp = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/default")
    assert resp.json()["roles"].keys() == {"default"}


def test_put_agent_without_roles_keeps_existing(client: TestClient) -> None:
    """An update that doesn't touch ``roles`` preserves what was there."""
    # Snapshot the seeded support_bot's roles first.
    before = client.get(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot"
    ).json()["roles"]
    assert before  # sanity: there's something to preserve

    # Touch only the system prompt.
    resp = client.put(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot",
        json={"system_md": "Updated prompt."},
    )
    assert resp.status_code == 200
    after = resp.json()
    assert after["system_md"] == "Updated prompt."
    assert after["roles"] == before


def test_list_agents_returns_roles_field(client: TestClient) -> None:
    """The /agents collection endpoint also surfaces the new field."""
    resp = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents")
    assert resp.status_code == 200
    agents = {a["name"]: a for a in resp.json()}
    assert "support_bot" in agents
    assert "roles" in agents["support_bot"]
    assert agents["default"]["roles"] == {}
