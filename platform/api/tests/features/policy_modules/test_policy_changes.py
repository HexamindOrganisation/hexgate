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
)
from hexgate_api.features.policy_modules.diff import diff_files
from hexgate_api.models import (
    AssistantChange,
    Organization,
    Project,
)
from hexgate_api.schemas import PolicyDiffResponse

OTHER_ORG_ID = "org_other"
OTHER_PROJECT_ID = "prj_other_org"
SIBLING_PROJECT_ID = "prj_sibling"

_COMPOSES = {"before": True, "after": True}
# The effect as the writer stores it: the diff of two file maps, dumped. This
# change tightens a grant, switches usage on and declares reach, so every list
# of the diff has an entry.
_EFFECT = PolicyDiffResponse(
    **diff_files(
        {
            "policy.yaml": "agents:\n  bot:\n    roles:\n      support:\n"
            "        tools: { refund: { mode: allow } }\n"
        },
        {
            "policy.yaml": "boundary: { tools: { refund: {} }, reach: { billing_bot: {} } }\n"
            "usage: { on_unavailable: deny }\n"
            "agents:\n  bot:\n    roles:\n      support:\n"
            "        tools: { refund: { mode: approval_required } }\n"
            "        reach: { billing_bot: {} }\n"
        },
    )
).model_dump(mode="json")

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
    "effect": _EFFECT,
}


async def _seed(session_factory, change_id: str, project_id: str, **overrides) -> None:
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
                **(_ROW | overrides),
            )
        )
        await s.commit()


def _get(
    client: TestClient, project_id: str, change_id: str, user: str = DEFAULT_USER_ID
):
    return client.get(
        f"/v1/projects/{project_id}/policy/changes/{change_id}",
        headers={"X-Dev-User": user},
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
    effect = r.json()["effect"]
    assert effect["agent_changes"] and effect["gate_changes"]
    assert any(c["before"] and c["after"] for c in effect["changes"])


def test_when_the_stored_effect_holds_only_the_required_keys_then_defaults_fill_it(
    client: TestClient, session_factory
) -> None:
    """A row stored before a list was added to the diff still reads back."""
    asyncio.run(
        _seed(
            session_factory,
            "chg_old",
            DEFAULT_PROJECT_ID,
            effect={"composes": _COMPOSES},
        )
    )

    r = _get(client, DEFAULT_PROJECT_ID, "chg_old")

    assert r.status_code == 200, r.text
    assert r.json()["effect"] == {
        "changes": [],
        "agent_changes": [],
        "gate_changes": [],
        "agents_affected": [],
        "composes": _COMPOSES,
    }


# An effect as a row stored today holds it, written out by hand and never
# regenerated: a later required, renamed or removed field in the diff models
# (top level or nested) fails the test below, as it would fail every such row.
_FROZEN_EFFECT = {
    "changes": [
        {
            "agent": "bot",
            "role": "support",
            "tool": "refund",
            "before": {"mode": "allow", "constraints": ["args.amount <= 1000"]},
            "after": {
                "mode": "approval_required",
                "constraints": ["args.amount <= 500"],
            },
            "constraints_added": ["args.amount <= 500"],
            "constraints_removed": ["args.amount <= 1000"],
        }
    ],
    "agent_changes": [
        {
            "agent": "bot",
            "key": "usage",
            "before": None,
            "after": {"on_unavailable": "deny"},
        }
    ],
    "gate_changes": [{"agent": "bot", "gate": "reach", "before": False, "after": True}],
    "agents_affected": ["bot"],
    "composes": _COMPOSES,
}


def _holds(read: object, stored: object) -> bool:
    """``read`` carries every key and value of ``stored``; a field added since,
    with its default, is allowed."""
    if isinstance(stored, dict):
        return isinstance(read, dict) and all(
            k in read and _holds(read[k], v) for k, v in stored.items()
        )
    if isinstance(stored, list):
        return (
            isinstance(read, list)
            and len(read) == len(stored)
            and all(_holds(r, v) for r, v in zip(read, stored))
        )
    return read == stored


def test_when_an_effect_stored_in_todays_shape_is_read_then_every_entry_survives(
    client: TestClient, session_factory
) -> None:
    asyncio.run(
        _seed(session_factory, "chg_old", DEFAULT_PROJECT_ID, effect=_FROZEN_EFFECT)
    )

    r = _get(client, DEFAULT_PROJECT_ID, "chg_old")

    assert r.status_code == 200, r.text
    assert _holds(r.json()["effect"], _FROZEN_EFFECT), r.json()["effect"]


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


def test_when_the_caller_is_a_plain_org_member_then_the_change_is_read(
    client: TestClient, session_factory, add_member: str
) -> None:
    """Reading a change is a read: any member of the org, not only its admins."""
    asyncio.run(_seed(session_factory, "chg_mine", DEFAULT_PROJECT_ID))

    r = _get(client, DEFAULT_PROJECT_ID, "chg_mine", user=add_member)

    assert r.status_code == 200, r.text
