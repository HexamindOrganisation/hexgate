"""Tests for the agent AI Act classification entry.

The endpoint pair is responsible for three things: round-tripping the
operator's assertion with server-side provenance, computing completeness (the
evidence report lists incomplete agents rather than omitting them), and
refusing to judge what the operator asserted. Plus the usual tenant gate — a
classification names an accountable person, so it must not leak across orgs.
"""

from __future__ import annotations

from datetime import datetime

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import (
    DEFAULT_ORG_ID,
    DEFAULT_PROJECT_ID,
    DEFAULT_USER_ID,
)
from hexgate_api.core import keystore as keystore_mod
from hexgate_api.core.db import get_session
from hexgate_api.core.ids import new_id
from hexgate_api.main import app
from hexgate_api.models import (
    Agent,
    AgentVersion,
    Organization,
    OrganizationMember,
    Project,
    User,
)
from hexgate_api.features.agents.service import upsert_classification
from hexgate_api.schemas import AgentClassificationWrite
from hexgate_api.seeds.defaults import ensure_default_project

_OTHER_ORG_ID = "dddddddd-0000-0000-0000-000000000001"
_OTHER_USER_ID = "dddddddd-0000-0000-0000-000000000002"
_OTHER_PROJECT_ID = "dddddddd-0000-0000-0000-000000000003"
_SECOND_MEMBER_ID = "eeeeeeee-0000-0000-0000-000000000001"
_SIBLING_PROJECT_ID = "eeeeeeee-0000-0000-0000-000000000002"

# Every test drives the seeded project's ``support_bot``.
_URL = f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/support_bot/classification"

_FULL_ENTRY = {
    "intended_purpose": "Triage inbound support tickets and issue refunds.",
    "operator_role": "deployer",
    "risk_tier": "not_high_risk",
    "oversight_owner_name": "Dana Okonkwo",
    "oversight_owner_contact": "dana@example.test",
    "checker_last_update_date": "2026-06-30",
}


@pytest_asyncio.fixture
async def session_factory():
    """Fresh in-memory async engine + factory, seeded with the triple-default.

    StaticPool so every session shares one connection — otherwise each
    ``:memory:`` handle gets its own private database.
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
    """TestClient authenticated as the seeded default user.

    The identity dependency builds its session-JWT strategy from the platform
    signing key and the fixture doesn't run the app lifespan, so wire a
    throwaway keystore here and restore the original afterwards.
    """
    from hexgate_api.core.keystore import FileKeyStore

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    original_keystore = keystore_mod.keystore
    keystore_mod.keystore = FileKeyStore(base_dir=tmp_path / "keystore")
    keystore_mod.keystore.ensure_keypair()
    try:
        yield TestClient(app, headers={"X-Dev-User": DEFAULT_USER_ID})
    finally:
        app.dependency_overrides.clear()
        keystore_mod.keystore = original_keystore


@pytest_asyncio.fixture
async def other_tenant(session_factory) -> str:
    """A second org with its own user and project. Returns the user id."""
    async with session_factory() as s:
        s.add(Organization(id=_OTHER_ORG_ID, slug="other-org", name="Other Org"))
        s.add(User(id=_OTHER_USER_ID, email="mallory@other.local"))
        s.add(
            OrganizationMember(
                user_id=_OTHER_USER_ID, org_id=_OTHER_ORG_ID, role="owner"
            )
        )
        s.add(Project(id=_OTHER_PROJECT_ID, org_id=_OTHER_ORG_ID, name="other"))
        await s.commit()
    return _OTHER_USER_ID


@pytest_asyncio.fixture
async def second_member(session_factory) -> str:
    """Another member of the SAME org — passes the gate, different identity."""
    async with session_factory() as s:
        s.add(User(id=_SECOND_MEMBER_ID, email="second@default.local"))
        s.add(
            OrganizationMember(
                user_id=_SECOND_MEMBER_ID, org_id=DEFAULT_ORG_ID, role="admin"
            )
        )
        await s.commit()
    return _SECOND_MEMBER_ID


@pytest_asyncio.fixture
async def sibling_project(session_factory) -> str:
    """A second project in the caller's own org, holding an agent of the same
    name — so only the project filter separates the two entries."""
    async with session_factory() as s:
        s.add(Project(id=_SIBLING_PROJECT_ID, org_id=DEFAULT_ORG_ID, name="sibling"))
        s.add(
            Agent(
                id=new_id(Agent),
                project_id=_SIBLING_PROJECT_ID,
                name="support_bot",
                agent_yaml="name: support_bot\n",
                policy_yaml="version: 1\ntools: {}\n",
            )
        )
        await s.commit()
    return _SIBLING_PROJECT_ID


def _moment(value: str) -> datetime:
    """Parse an API timestamp, dropping the offset.

    SQLite hands a re-read back naive while the in-memory value is tz-aware,
    so the two are not directly comparable in dev; on Postgres both are aware.
    Dropping tzinfo makes the comparison mean the same thing on both.
    """
    return datetime.fromisoformat(value).replace(tzinfo=None)


async def _seeded_agent_id(session_factory) -> str:
    async with session_factory() as s:
        agent = (
            await s.exec(
                select(Agent).where(
                    Agent.project_id == DEFAULT_PROJECT_ID,
                    Agent.name == "support_bot",
                )
            )
        ).first()
    return agent.id


async def _register_manifest_description(
    session_factory, description: str | None
) -> None:
    """Attach an AgentVersion carrying ``description`` to the seeded agent.

    Writes the rows directly rather than going through ``POST /v1/agents``,
    which is bearer-authed; the prefill reads the stored manifest either way.
    """
    agent_id = await _seeded_agent_id(session_factory)
    async with session_factory() as s:
        s.add(
            AgentVersion(
                id=new_id(AgentVersion),
                agent_id=agent_id,
                version=1,
                content_hash="hash-1",
                manifest={
                    "name": "support_bot",
                    "description": description,
                    "framework": "hexgate",
                    "tools": [],
                },
            )
        )
        await s.commit()


# ---------------------------------------------------------------------------
# GET — the empty entry, and the manifest prefill
# ---------------------------------------------------------------------------


def test_get_classification_happy_path(client: TestClient) -> None:
    """An agent nobody has classified reads as an unrecorded, incomplete entry."""
    resp = client.get(_URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_name"] == "support_bot"
    assert body["recorded"] is False
    assert body["complete"] is False
    assert body["recorded_by_user_id"] is None
    assert body["recorded_at"] is None
    assert body["missing_fields"] == [
        "intended_purpose",
        "operator_role",
        "risk_tier",
        "oversight_owner_name",
    ]


async def test_when_manifest_has_description_then_purpose_is_prefilled(
    session_factory, client: TestClient
) -> None:
    """With no entry yet, the registered manifest's description opens the form."""
    await _register_manifest_description(session_factory, "Handles refunds.")

    body = client.get(_URL).json()
    assert body["intended_purpose"] == "Handles refunds."
    # Suggested, not asserted: nothing is recorded until the operator PUTs.
    assert body["recorded"] is False
    assert "intended_purpose" in body["missing_fields"]


async def test_when_an_entry_exists_then_the_manifest_never_fills_its_purpose(
    session_factory, client: TestClient
) -> None:
    """A recorded entry shows only what the operator recorded.

    Filling a null purpose here would put the SDK manifest's developer-written
    blurb on screen under ``recorded: true``, and the operator's next Save
    would stamp it as their own Art. 3(12) assertion.
    """
    await _register_manifest_description(session_factory, "Handles refunds.")
    client.put(_URL, json={"operator_role": "deployer"})

    body = client.get(_URL).json()
    assert body["recorded"] is True
    assert body["intended_purpose"] is None
    assert "intended_purpose" in body["missing_fields"]


def test_when_agent_is_unknown_then_get_is_404(client: TestClient) -> None:
    resp = client.get(f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/nope/classification")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT — recording the operator's assertion
# ---------------------------------------------------------------------------


def test_put_classification_happy_path(client: TestClient) -> None:
    """A full entry round-trips, is complete, and is stamped with who + when."""
    resp = client.put(_URL, json=_FULL_ENTRY)
    assert resp.status_code == 200
    body = resp.json()
    for field, value in _FULL_ENTRY.items():
        assert body[field] == value
    assert body["recorded"] is True
    assert body["complete"] is True
    assert body["missing_fields"] == []
    assert body["recorded_by_user_id"] == DEFAULT_USER_ID
    assert body["recorded_at"] is not None

    # And it survives a re-read. ``recorded_at`` is compared separately: the
    # PUT answers from the in-memory value and SQLite hands the re-read back
    # naive, so the two strings differ by the offset suffix in local dev only
    # (Postgres stores the column ``timezone=True``).
    reread = client.get(_URL).json()
    assert reread.pop("recorded_at").startswith(body["recorded_at"][:19])
    assert reread == {k: v for k, v in body.items() if k != "recorded_at"}


def test_when_put_twice_then_the_entry_is_replaced_not_merged(
    client: TestClient,
) -> None:
    """PUT is a full replace — a field left out is cleared, not carried over."""
    client.put(_URL, json=_FULL_ENTRY)

    resp = client.put(
        _URL,
        json={"intended_purpose": "Ticket triage only.", "operator_role": "provider"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["intended_purpose"] == "Ticket triage only."
    assert body["operator_role"] == "provider"
    assert body["risk_tier"] is None
    assert body["oversight_owner_name"] is None
    assert body["checker_last_update_date"] is None
    assert body["complete"] is False


def test_when_entry_is_partial_then_missing_fields_name_what_is_left(
    client: TestClient,
) -> None:
    """A partial save is accepted and reported as incomplete, never rejected."""
    resp = client.put(_URL, json={"intended_purpose": "Ticket triage."})
    assert resp.status_code == 200
    body = resp.json()
    assert body["recorded"] is True
    assert body["complete"] is False
    assert body["missing_fields"] == [
        "operator_role",
        "risk_tier",
        "oversight_owner_name",
    ]


def test_when_tier_is_high_risk_then_annex_iii_point_is_required(
    client: TestClient,
) -> None:
    """High risk without an Annex III point is incomplete; with one, complete."""
    high_risk = {**_FULL_ENTRY, "risk_tier": "high_risk"}
    body = client.put(_URL, json=high_risk).json()
    assert body["complete"] is False
    assert body["missing_fields"] == ["annex_iii_point"]

    body = client.put(_URL, json={**high_risk, "annex_iii_point": "5(b)"}).json()
    assert body["complete"] is True
    assert body["annex_iii_point"] == "5(b)"


def test_when_high_risk_is_downgraded_then_the_annex_point_stops_being_required(
    client: TestClient,
) -> None:
    """Only the high-risk tier pulls the Annex III point into completeness."""
    client.put(
        _URL, json={**_FULL_ENTRY, "risk_tier": "high_risk", "annex_iii_point": "5(b)"}
    )

    body = client.put(_URL, json={**_FULL_ENTRY, "risk_tier": "minimal"}).json()
    assert body["complete"] is True
    assert body["annex_iii_point"] is None


@pytest.mark.parametrize(
    "field",
    [
        "intended_purpose",
        "oversight_owner_name",
        "annex_iii_point",
        "oversight_owner_contact",
    ],
)
def test_when_a_field_is_blank_then_it_reads_as_missing(
    client: TestClient, field: str
) -> None:
    """Whitespace is not an assertion — blanks normalise to null on every field.

    ``missing_fields`` tests truthiness, so a whitespace-only purpose that
    survived normalisation would read as a *complete* Art. 3(12) assertion.
    """
    entry = {**_FULL_ENTRY, "risk_tier": "high_risk", "annex_iii_point": "5(b)"}
    body = client.put(_URL, json={**entry, field: "   "}).json()
    assert body[field] is None
    # Blanking a completeness field moves it back into missing_fields. The
    # tier here is high_risk, so annex_iii_point is one of them; only
    # oversight_owner_contact sits outside the rule.
    expected = [] if field == "oversight_owner_contact" else [field]
    assert body["missing_fields"] == expected


def test_when_assertion_looks_inconsistent_then_it_is_still_recorded(
    client: TestClient,
) -> None:
    """We record what the operator asserts — no opinion, no warning field.

    ``support_bot`` grants refunds across four roles; calling it "minimal" risk
    is the operator's call to defend, not ours to block.
    """
    body = client.put(_URL, json={**_FULL_ENTRY, "risk_tier": "minimal"}).json()
    assert body["risk_tier"] == "minimal"
    assert body["complete"] is True
    assert "warnings" not in body


@pytest.mark.parametrize(
    "field,value",
    [("risk_tier", "kind-of-risky"), ("operator_role", "auditor")],
)
def test_when_a_closed_set_field_is_unknown_then_put_is_422(
    client: TestClient, field: str, value: str
) -> None:
    """Tier and role are closed sets — completeness compares them by exact string."""
    resp = client.put(_URL, json={**_FULL_ENTRY, field: value})
    assert resp.status_code == 422


def test_when_the_body_asserts_nothing_then_put_is_422(client: TestClient) -> None:
    """An empty body would name a recorder against zero assertions — and, since
    PUT replaces, wipe a complete entry if the form submitted before it loaded."""
    client.put(_URL, json=_FULL_ENTRY)

    assert client.put(_URL, json={}).status_code == 422
    # The earlier assertion is untouched.
    assert client.get(_URL).json()["complete"] is True


def test_when_a_field_name_is_misspelled_then_put_is_422(client: TestClient) -> None:
    """Under replace semantics an ignored key is silent data loss, not a no-op."""
    client.put(
        _URL, json={**_FULL_ENTRY, "risk_tier": "high_risk", "annex_iii_point": "5(b)"}
    )

    resp = client.put(_URL, json={**_FULL_ENTRY, "risk_teir": "high_risk"})
    assert resp.status_code == 422
    assert client.get(_URL).json()["risk_tier"] == "high_risk"


def test_when_agent_is_unknown_then_put_is_404(client: TestClient) -> None:
    resp = client.put(
        f"/v1/projects/{DEFAULT_PROJECT_ID}/agents/nope/classification",
        json=_FULL_ENTRY,
    )
    assert resp.status_code == 404


def test_when_two_first_saves_race_then_the_later_one_wins(
    client: TestClient, second_member: str, monkeypatch
) -> None:
    """A lost INSERT race is recovered: 200, later assertion, later recorder.

    Simulates the double-clicked Save, or two members saving at once: the
    second request reads "no entry" (its ``get_classification`` is stubbed to
    the stale answer the race gives it), inserts, and hits
    ``uq_agent_classification``.

    Driven through the endpoint for the status code and the durability of the
    recovered write. What the *savepoint* buys is pinned separately by
    ``test_when_a_save_races_then_objects_loaded_earlier_stay_usable``.
    """
    client.put(_URL, json={"intended_purpose": "First writer."})

    from hexgate_api.features.agents import service

    real_get = service.get_classification
    calls = {"n": 0}

    async def stale_once(session, agent_id):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real_get(session, agent_id)

    monkeypatch.setattr(service, "get_classification", stale_once)
    resp = client.put(
        _URL,
        json={"intended_purpose": "Second writer."},
        headers={"X-Dev-User": second_member},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["intended_purpose"] == "Second writer."
    # The recovery re-stamps provenance too — the values and the name that
    # answers for them have to come from the same writer.
    assert body["recorded_by_user_id"] == second_member
    monkeypatch.undo()
    # And the recovered write is committed, not just returned.
    reread = client.get(_URL).json()
    assert reread["intended_purpose"] == "Second writer."
    assert reread["recorded_by_user_id"] == second_member


async def test_when_a_save_races_then_objects_loaded_earlier_stay_usable(
    session_factory,
) -> None:
    """The recovery must not disturb the rest of the request's session.

    A plain ``session.rollback()`` would expire every object already loaded on
    it — the route loads an ``Agent`` before calling this — and the next
    attribute read would then raise MissingGreenlet inside the response. The
    SAVEPOINT rolls back only the failed INSERT. Asserted here rather than
    through the route so it holds for any future caller, not just the one
    whose read happens to exist today.
    """
    from hexgate_api.features.agents import service

    agent_id = await _seeded_agent_id(session_factory)
    async with session_factory() as s:
        await service.upsert_classification(
            s,
            agent_id=agent_id,
            recorded_by_user_id=DEFAULT_USER_ID,
            values=AgentClassificationWrite(intended_purpose="First writer."),
        )

    async with session_factory() as s:
        # An object the caller loaded before the write, as a route would.
        agent = await service.get_agent(s, DEFAULT_PROJECT_ID, "support_bot")

        real_get = service.get_classification
        calls = {"n": 0}

        async def stale_once(session, aid):
            calls["n"] += 1
            return None if calls["n"] == 1 else await real_get(session, aid)

        service.get_classification = stale_once
        try:
            await service.upsert_classification(
                s,
                agent_id=agent_id,
                recorded_by_user_id=DEFAULT_USER_ID,
                values=AgentClassificationWrite(intended_purpose="Second writer."),
            )
        finally:
            service.get_classification = real_get

        # Would raise MissingGreenlet if the recovery had expired it.
        assert agent.name == "support_bot"


def test_when_another_member_overwrites_then_provenance_is_restamped(
    client: TestClient, second_member: str
) -> None:
    """The entry names whoever last asserted it, not whoever created it.

    Art. 6(4) wants the assessment attributed; an update that kept the first
    recorder's name would attribute B's assertion to A.
    """
    first = client.put(_URL, json=_FULL_ENTRY).json()
    assert first["recorded_by_user_id"] == DEFAULT_USER_ID

    second = client.put(
        _URL,
        json={**_FULL_ENTRY, "risk_tier": "minimal"},
        headers={"X-Dev-User": second_member},
    ).json()
    assert second["recorded_by_user_id"] == second_member
    # Parsed, and strictly greater. Comparing the ISO strings would pass on a
    # dropped re-stamp: on Postgres both sides are tz-aware and equal, and on
    # SQLite the re-read loses the "Z" so the shorter string sorts first.
    assert _moment(second["recorded_at"]) > _moment(first["recorded_at"])


def test_when_an_entry_is_updated_then_the_update_is_readable(
    client: TestClient,
) -> None:
    """An accepted update reaches the database, not just the response body.

    Every other update assertion reads the PUT's own response, which is built
    from the in-memory row — so without this, dropping the update path's
    ``commit`` would return 200 with the right body and silently roll back.
    """
    client.put(_URL, json=_FULL_ENTRY)
    client.put(_URL, json={**_FULL_ENTRY, "intended_purpose": "Updated."})

    assert client.get(_URL).json()["intended_purpose"] == "Updated."


def test_when_two_projects_share_an_agent_name_then_entries_stay_separate(
    client: TestClient, sibling_project: str
) -> None:
    """The agent lookup is bound to the project in the URL, not the name alone.

    Both projects are in the caller's own org, so the org gate passes for
    either: the only thing keeping the two ``support_bot`` entries apart is
    ``get_agent``'s project filter.
    """
    sibling_url = f"/v1/projects/{sibling_project}/agents/support_bot/classification"
    client.put(_URL, json={**_FULL_ENTRY, "intended_purpose": "Default project."})
    client.put(sibling_url, json={**_FULL_ENTRY, "intended_purpose": "Sibling."})

    assert client.get(_URL).json()["intended_purpose"] == "Default project."
    assert client.get(sibling_url).json()["intended_purpose"] == "Sibling."


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_when_caller_is_outside_the_org_then_classification_is_403(
    client: TestClient, other_tenant: str
) -> None:
    """A member of another org can neither read nor record this entry."""
    headers = {"X-Dev-User": other_tenant}
    assert client.get(_URL, headers=headers).status_code == 403
    assert client.put(_URL, json=_FULL_ENTRY, headers=headers).status_code == 403


# ---------------------------------------------------------------------------
# The provenance FK, on a database that enforces it
# ---------------------------------------------------------------------------


async def test_when_a_recorder_is_deleted_then_the_delete_is_refused() -> None:
    """``recorded_by_user_id`` blocks deleting an operator who asserted something.

    The deliberate counterpart to the display-only ``actor_fk_column()``
    columns, which go NULL instead (tests/features/tokens/test_tokens.py). An
    assertion with no asserter is not an assertion, so the FK refuses rather
    than quietly dropping the Art. 6(4) name — and offboarding has to decide
    what becomes of that operator's entries.

    Needs its own engine: SQLite ignores foreign keys unless the pragma is set
    per connection, so the rest of this file would never see the constraint.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _pragma(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as bootstrap:
        await ensure_default_project(bootstrap)

    try:
        async with factory() as s:
            recorder = User(email="leaver@example.test")
            s.add(recorder)
            await s.commit()
            agent = (
                await s.exec(
                    select(Agent).where(
                        Agent.project_id == DEFAULT_PROJECT_ID,
                        Agent.name == "support_bot",
                    )
                )
            ).first()
            await upsert_classification(
                s,
                agent_id=agent.id,
                recorded_by_user_id=recorder.id,
                values=AgentClassificationWrite(intended_purpose="Asserted by them."),
            )

            await s.delete(recorder)
            with pytest.raises(IntegrityError):
                await s.commit()
    finally:
        await engine.dispose()
