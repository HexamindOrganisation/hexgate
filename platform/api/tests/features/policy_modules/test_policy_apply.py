"""POST /policy/apply: who may apply a change, and how its refusals answer.

The checks and the transaction behind the route have their own tests
(``test_apply.py``); these call the route over HTTP and hold its gates: an
admin of the project, a token marked ``policy:apply``, and the org's
assistant mode for the assistant's own client, and no other.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from hexgate_api.constants import (
    ASSISTANT_CLIENT_ID,
    DEFAULT_ORG_ID,
    DEFAULT_PROJECT_ID,
    DEFAULT_USER_ID,
)
from hexgate_api.features.policy_modules import service
from hexgate_api.models import AssistantChange, Organization
from tests.features.oauth.conftest import mint

PID = DEFAULT_PROJECT_ID
URL = f"/v1/projects/{PID}/policy/apply"
OTHER_CLIENT = "https://client.example/meta.json"

_ENTRY = """\
boundary:
  tools:
    refund: { mode: allow, constraint: 'args.amount <= 1000' }
agents:
  bot:
    roles:
      support: { import: [ grants.yaml ] }
"""
_GRANTS = "tools:\n  refund: { mode: allow, constraint: 'args.amount <= 500' }\n"
_NEW_GRANTS = "tools:\n  refund: { mode: allow, constraint: 'args.amount <= 200' }\n"
_SAMPLE = {
    "agent": "bot",
    "role": "support",
    "tool": "refund",
    "args": {"amount": 300},
    "expect": "deny",
}


@pytest.fixture
def stored(session_factory) -> dict[str, str]:
    """The project's saved files, which no refused apply may change."""
    files = {"policy.yaml": _ENTRY, "grants.yaml": _GRANTS}

    async def seed() -> None:
        async with session_factory() as session:
            for name, content in files.items():
                await service.upsert_file(
                    session,
                    project_id=PID,
                    name=name,
                    content=content,
                    actor_user_id=DEFAULT_USER_ID,
                )

    asyncio.run(seed())
    return files


def _body(content: str = _NEW_GRANTS, base: str | None = _GRANTS) -> dict:
    base_hash = None if base is None else service.text_hash(base)
    return {
        "files": [{"name": "grants.yaml", "content": content, "base_hash": base_hash}],
        "summary": "refunds capped at 200",
        "samples": [_SAMPLE],
    }


def _bearer(client_id: str = ASSISTANT_CLIENT_ID, scopes=("policy:apply",)) -> dict:
    token = mint(client_id=client_id, scopes=list(scopes))
    return {"Authorization": f"Bearer {token}"}


def _headers(client_id: str | None) -> dict:
    """A dashboard user's headers for None, else a token issued to ``client_id``."""
    return {"X-Dev-User": DEFAULT_USER_ID} if client_id is None else _bearer(client_id)


def _set_mode(session_factory, mode: str) -> None:
    async def write() -> None:
        async with session_factory() as session:
            org = await session.get(Organization, DEFAULT_ORG_ID)
            org.assistant_mode = mode
            session.add(org)
            await session.commit()

    asyncio.run(write())


def _saved(session_factory) -> tuple[dict[str, str], list[AssistantChange]]:
    async def read():
        async with session_factory() as session:
            files = await service.list_files(session, PID)
            changes = (await session.exec(select(AssistantChange))).all()
            return {f.name: f.content for f in files}, list(changes)

    return asyncio.run(read())


def _applied(r) -> str:
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True, r.json()
    return r.json()["change_id"]


def _refused(r, session_factory, stored, status: int, detail=None) -> None:
    """``r`` answered ``status`` (and ``detail``, when given), nothing saved."""
    assert r.status_code == status, r.text
    if detail is not None:
        assert r.json()["detail"] == detail
    assert _saved(session_factory) == (stored, [])


@pytest.mark.parametrize(
    "client_id",
    [
        pytest.param(None, id="dashboard"),
        pytest.param(ASSISTANT_CLIENT_ID, id="assistant"),
        pytest.param(OTHER_CLIENT, id="other-client"),
    ],
)
def test_api_apply_policy_happy_path(
    client: TestClient, session_factory, stored, client_id
) -> None:
    """In a read_write org (the default) every caller applies, and the change
    row records the user and the token's client."""
    r = client.post(URL, json=_body(), headers=_headers(client_id))

    change_id = _applied(r)
    files, changes = _saved(session_factory)
    assert files == stored | {"grants.yaml": _NEW_GRANTS}
    assert [(c.id, c.created_by_user_id, c.client_id) for c in changes] == [
        (change_id, DEFAULT_USER_ID, client_id)
    ]
    assert [c["id"] for c in r.json()["checks"]][-1] == "bundle"


@pytest.mark.parametrize("mode", ["read_only", "off", "unknown"])
def test_when_the_assistant_applies_in_an_org_that_does_not_let_it_write_then_403(
    client: TestClient, session_factory, stored, mode
) -> None:
    """Its token may predate the mode change, so it still carries the scope."""
    _set_mode(session_factory, mode)

    r = client.post(URL, json=_body(), headers=_bearer())

    _refused(r, session_factory, stored, 403, {"code": "assistant_mode"})


@pytest.mark.parametrize("mode", ["read_only", "off"])
@pytest.mark.parametrize(
    "client_id",
    [pytest.param(None, id="dashboard"), pytest.param(OTHER_CLIENT, id="other-client")],
)
def test_when_another_caller_applies_in_an_org_closed_to_the_assistant_then_it_passes(
    client: TestClient, session_factory, stored, mode, client_id
) -> None:
    """The org's assistant setting governs the assistant alone, not Claude Code."""
    _set_mode(session_factory, mode)

    _applied(client.post(URL, json=_body(), headers=_headers(client_id)))


@pytest.mark.parametrize(
    "base",
    [pytest.param(_GRANTS + "# edited\n", id="edited"), pytest.param(None, id="null")],
)
def test_when_a_base_hash_is_stale_then_409_naming_the_file(
    client: TestClient, session_factory, stored, base
) -> None:
    """A comment-only edit is stale too; a null base claims the file is new."""
    r = client.post(URL, json=_body(base=base), headers=_bearer())

    detail = {"code": "stale", "files": ["grants.yaml"]}
    _refused(r, session_factory, stored, 409, detail)


def test_when_the_project_has_no_entry_file_then_409_layout(
    client: TestClient, session_factory
) -> None:
    r = client.post(URL, json=_body(base=None), headers=_bearer())

    _refused(r, session_factory, {}, 409, {"code": "layout"})


def test_when_a_check_fails_then_200_not_applied_and_nothing_written(
    client: TestClient, session_factory, stored
) -> None:
    """The client needs the structured failures, not an error status."""
    r = client.post(URL, json=_body(content="tools: [unclosed\n"), headers=_bearer())

    _refused(r, session_factory, stored, 200)
    assert (r.json()["applied"], r.json()["change_id"]) == (False, None)
    assert r.json()["checks"][0]["status"] == "fail"


def test_when_the_bundles_cannot_be_built_then_200_not_applied_and_rolled_back(
    client: TestClient, session_factory, stored, monkeypatch
) -> None:
    """The rollback expires what the request's session loaded, the caller
    included: the route still answers, from what it read before."""

    async def unbuilt(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service, "rebuild_bundles", unbuilt)

    r = client.post(URL, json=_body(), headers=_bearer())

    _refused(r, session_factory, stored, 200)
    assert r.json()["applied"] is False
    assert r.json()["checks"][-1]["id"] == "bundle"
    assert r.json()["checks"][-1]["status"] == "fail"


def test_when_the_caller_is_a_plain_org_member_then_403(
    client: TestClient, session_factory, stored, add_member: str
) -> None:
    r = client.post(URL, json=_body(), headers={"X-Dev-User": add_member})

    _refused(r, session_factory, stored, 403)


@pytest.mark.parametrize("scopes", [("policy:write",), ("policy:read",)])
def test_when_the_token_lacks_policy_apply_then_403(
    client: TestClient, session_factory, stored, scopes
) -> None:
    """``policy:write`` reaches the plain file PUT, never the checked apply."""
    r = client.post(URL, json=_body(), headers=_bearer(OTHER_CLIENT, scopes))

    _refused(r, session_factory, stored, 403)
