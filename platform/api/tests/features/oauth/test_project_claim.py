"""The token's ``projects`` claim on the marked routes.

The grant is narrower than the permission: the default user owns the default
org (two projects) and a second org, but a token names only some of their
projects. And the claim is not a permission: a token naming a project of an org
the user left, or saving as a plain member, is still refused by the route's own
check.
"""

from __future__ import annotations

import shutil
import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlmodel import select

from hexgate_api.constants import (
    DEFAULT_ORG_ID,
    DEFAULT_PROJECT_ID,
    DEFAULT_USER_ID,
    ROLE_OWNER,
)
from hexgate_api.models import (
    Organization,
    OrganizationMember,
    PolicyFile,
    Project,
    User,
)
from tests.features.oauth.conftest import mint

OTHER_PROJECT_ID = "00000000-0000-0000-0000-0000000000aa"
OTHER_ORG_ID = "00000000-0000-0000-0000-0000000000bb"
# An org the user is not a member of, whose project a token still names (a user
# who left it keeps a live token for up to ten minutes).
LEFT_ORG_ID = "00000000-0000-0000-0000-0000000000cc"
LEFT_PROJECT_ID = "00000000-0000-0000-0000-0000000000dd"


@pytest_asyncio.fixture
async def other_project(session_factory) -> None:
    """A second project in the default org, another owned org with a project, and an org left."""
    async with session_factory() as s:
        s.add(Project(id=OTHER_PROJECT_ID, org_id=DEFAULT_ORG_ID, name="other"))
        s.add(Organization(id=OTHER_ORG_ID, slug="other", name="Other"))
        await s.commit()
        s.add(Project(id=str(uuid.uuid4()), org_id=OTHER_ORG_ID, name="elsewhere"))
        s.add(Organization(id=LEFT_ORG_ID, slug="left", name="Left"))
        await s.commit()
        s.add(Project(id=LEFT_PROJECT_ID, org_id=LEFT_ORG_ID, name="left"))
        s.add(
            OrganizationMember(
                user_id=DEFAULT_USER_ID, org_id=OTHER_ORG_ID, role=ROLE_OWNER
            )
        )
        await s.commit()


def _get(client: TestClient, url: str, token: str | None = None):
    if token is None:
        return client.get(url, headers={"X-Dev-User": DEFAULT_USER_ID})
    return client.get(url, headers={"Authorization": f"Bearer {token}"})


def test_when_token_names_the_project_then_the_route_answers_200(
    client: TestClient,
) -> None:
    token = mint(projects=[DEFAULT_PROJECT_ID])
    url = f"/v1/projects/{DEFAULT_PROJECT_ID}/policy-files"
    assert _get(client, url, token).status_code == 200


@pytest.mark.usefixtures("other_project")
def test_when_token_does_not_name_the_project_then_403_though_the_user_owns_it(
    client: TestClient,
) -> None:
    token = mint(projects=[DEFAULT_PROJECT_ID])
    url = f"/v1/projects/{OTHER_PROJECT_ID}/policy-files"
    r = _get(client, url, token)
    assert r.status_code == 403
    assert r.json()["detail"] == "token does not cover this project"
    # The same user reaches it from the dashboard: only the grant is narrower.
    assert _get(client, url).status_code == 200


@pytest.mark.usefixtures("other_project")
def test_when_caller_is_a_token_then_list_orgs_keeps_the_orgs_of_its_projects(
    client: TestClient,
) -> None:
    """Only orgs the user belongs to, and of those only the ones the token names a project in."""
    token = mint(
        scopes=["projects:read"], projects=[DEFAULT_PROJECT_ID, LEFT_PROJECT_ID]
    )
    r = _get(client, "/v1/orgs", token)
    assert r.status_code == 200, r.text
    assert [o["id"] for o in r.json()] == [DEFAULT_ORG_ID]
    dashboard = _get(client, "/v1/orgs").json()
    assert {o["id"] for o in dashboard} == {DEFAULT_ORG_ID, OTHER_ORG_ID}


@pytest.mark.usefixtures("other_project")
def test_when_caller_is_a_token_then_list_projects_keeps_its_projects(
    client: TestClient,
) -> None:
    token = mint(scopes=["projects:read"], projects=[DEFAULT_PROJECT_ID])
    url = f"/v1/orgs/{DEFAULT_ORG_ID}/projects"
    r = _get(client, url, token)
    assert r.status_code == 200, r.text
    assert [p["id"] for p in r.json()] == [DEFAULT_PROJECT_ID]
    dashboard = _get(client, url).json()
    assert {p["id"] for p in dashboard} == {DEFAULT_PROJECT_ID, OTHER_PROJECT_ID}


async def test_when_token_user_has_no_org_then_list_orgs_creates_none(
    client: TestClient, session_factory
) -> None:
    """The personal-org repair is a write; a read scope must not trigger it."""
    async with session_factory() as s:
        user = User(email="orgless@example.com")
        s.add(user)
        await s.commit()
        user_id = user.id
    token = mint(sub=user_id, scopes=["projects:read"], projects=[DEFAULT_PROJECT_ID])
    r = _get(client, "/v1/orgs", token)
    assert r.status_code == 200, r.text
    assert r.json() == []
    async with session_factory() as s:
        rows = (
            await s.exec(
                select(OrganizationMember).where(OrganizationMember.user_id == user_id)
            )
        ).all()
    assert rows == []


@pytest.mark.usefixtures("other_project")
def test_when_token_names_a_project_of_an_org_left_then_membership_still_refuses(
    client: TestClient,
) -> None:
    token = mint(projects=[LEFT_PROJECT_ID])
    r = _get(client, f"/v1/projects/{LEFT_PROJECT_ID}/policy-files", token)
    assert r.status_code == 403
    assert r.json()["detail"] == "not a member of this org"


_SAVE_URL = f"/v1/projects/{DEFAULT_PROJECT_ID}/policy-files/policy.yaml"
_SAVE_BODY = {"content": "tools: { a: { mode: allow } }\n"}
needs_opa = pytest.mark.skipif(shutil.which("opa") is None, reason="opa not on PATH")


async def test_when_a_member_token_saves_a_policy_file_then_403_admin_required(
    client: TestClient, session_factory
) -> None:
    async with session_factory() as s:
        member = User(email="member@example.com")
        s.add(member)
        await s.commit()
        s.add(
            OrganizationMember(user_id=member.id, org_id=DEFAULT_ORG_ID, role="member")
        )
        await s.commit()
        member_id = member.id
    token = mint(sub=member_id, scopes=["policy:write"])
    r = client.put(
        _SAVE_URL, json=_SAVE_BODY, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "admin or owner role required for this action"


@needs_opa  # saving the entry file compiles the project's bundle
async def test_when_an_owner_token_saves_a_policy_file_then_200_and_the_owner_is_the_actor(
    client: TestClient, session_factory
) -> None:
    token = mint(scopes=["policy:write"])
    r = client.put(
        _SAVE_URL, json=_SAVE_BODY, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200, r.text
    async with session_factory() as s:
        saved = (
            await s.exec(select(PolicyFile).where(PolicyFile.name == "policy.yaml"))
        ).one()
    assert saved.created_by_user_id == DEFAULT_USER_ID
