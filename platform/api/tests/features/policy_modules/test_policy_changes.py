"""GET /policy/changes/{change_id}: one applied change, read back by its id.

The row holds every changed file whole, before and after, so what these tests
hold is the scoping: the id alone never reaches a change of another project,
and an id from elsewhere answers exactly as an unknown one does.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from hexgate_api.constants import (
    DEFAULT_ORG_ID,
    DEFAULT_PROJECT_ID,
    DEFAULT_USER_ID,
    ROLE_MEMBER,
)
from hexgate_api.models import (
    AssistantChange,
    Organization,
    OrganizationMember,
    Project,
    User,
)

ME = {"X-Dev-User": DEFAULT_USER_ID}
OTHER_ORG_ID = "org_other"
OTHER_PROJECT_ID = "prj_other_org"
SIBLING_PROJECT_ID = "prj_sibling"
MEMBER_ID = "usr_member"

# Every stored field but the id, the project and the time, as the API returns it.
_ROW = {
    "session_id": "ses_1",
    "created_by_user_id": DEFAULT_USER_ID,
    "client_id": "hexgate-dashboard-assistant",
    "summary": "Lower the billing refund cap to 500",
    "files": [
        {
            "name": "caps/billing/refund_billing.yaml",
            "before_content": "tools: { refund_order: { constraint: 'args.amount <= 1000' } }\n",
            "before_hash": "a" * 64,
            "after_content": "tools: { refund_order: { constraint: 'args.amount <= 500' } }\n",
            "after_hash": "b" * 64,
        },
        # A file the change created has no before side.
        {
            "name": "caps/billing/new.yaml",
            "before_content": None,
            "before_hash": None,
            "after_content": "tools: {}\n",
            "after_hash": "c" * 64,
        },
    ],
    "samples": [
        {
            "agent": "*",
            "role": "billing",
            "tool": "refund_order",
            "args": {"amount": 800},
            "attributes": {},
            "expect": "deny",
        }
    ],
    "checks": [{"id": "yaml", "status": "pass", "detail": "", "items": []}],
    # Returned as recorded, keys the reader does not know included.
    "boundary_changes": [
        {"file": "policy.yaml", "path": "x", "kind": "tightened", "extra": 1}
    ],
    "effect": {
        "changes": [],
        "agent_changes": [],
        "gate_changes": [],
        "agents_affected": ["billing-bot"],
        "composes": {"before": True, "after": True},
    },
}


async def _seed(session_factory, change_id: str, project_id: str) -> None:
    """A sibling project in the caller's org, an org the caller is not in, and
    one change stored under ``project_id``."""
    async with session_factory() as s:
        s.add(Project(id=SIBLING_PROJECT_ID, org_id=DEFAULT_ORG_ID, name="sibling"))
        s.add(Organization(id=OTHER_ORG_ID, slug="other", name="Other"))
        s.add(Project(id=OTHER_PROJECT_ID, org_id=OTHER_ORG_ID, name="elsewhere"))
        s.add(
            AssistantChange(
                id=change_id,
                project_id=project_id,
                created_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
                **_ROW,
            )
        )
        await s.commit()


def _get(client: TestClient, project_id: str, change_id: str):
    return client.get(
        f"/v1/projects/{project_id}/policy/changes/{change_id}", headers=ME
    )


def test_api_read_policy_change_happy_path(client: TestClient, session_factory) -> None:
    asyncio.run(_seed(session_factory, "chg_mine", DEFAULT_PROJECT_ID))

    r = _get(client, DEFAULT_PROJECT_ID, "chg_mine")

    assert r.status_code == 200, r.text
    assert r.json() == {
        **_ROW,
        "id": "chg_mine",
        "project_id": DEFAULT_PROJECT_ID,
        "created_at": "2026-11-19T10:00:00Z",
    }


@pytest.mark.parametrize(
    "owner_project",
    [OTHER_PROJECT_ID, SIBLING_PROJECT_ID],
    ids=["other-org", "same-org"],
)
def test_when_the_change_is_another_projects_then_404_as_if_unknown(
    client: TestClient, session_factory, owner_project: str
) -> None:
    """Asked through a project the caller may read, a change stored under any
    other project answers exactly as an id that does not exist."""
    asyncio.run(_seed(session_factory, "chg_theirs", owner_project))

    theirs = _get(client, DEFAULT_PROJECT_ID, "chg_theirs")
    unknown = _get(client, DEFAULT_PROJECT_ID, "chg_nope")

    assert unknown.status_code == 404, unknown.text
    assert (theirs.status_code, theirs.json()) == (404, unknown.json())


def test_when_asked_through_the_owning_project_of_another_org_then_403(
    client: TestClient, session_factory
) -> None:
    """The direct route to another org's change: its own project id in the path."""
    asyncio.run(_seed(session_factory, "chg_theirs", OTHER_PROJECT_ID))

    r = _get(client, OTHER_PROJECT_ID, "chg_theirs")

    assert r.status_code == 403, r.text


async def _add_member(session_factory) -> str:
    async with session_factory() as s:
        s.add(User(id=MEMBER_ID, email="member@example.test"))
        s.add(
            OrganizationMember(
                user_id=MEMBER_ID, org_id=DEFAULT_ORG_ID, role=ROLE_MEMBER
            )
        )
        await s.commit()
    return MEMBER_ID


def test_when_the_caller_is_a_plain_org_member_then_the_change_is_read(
    client: TestClient, session_factory
) -> None:
    """Reading a change is a read: any member of the org, not only its admins."""
    asyncio.run(_seed(session_factory, "chg_mine", DEFAULT_PROJECT_ID))
    member = asyncio.run(_add_member(session_factory))

    r = client.get(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/policy/changes/chg_mine",
        headers={"X-Dev-User": member},
    )

    assert r.status_code == 200, r.text
