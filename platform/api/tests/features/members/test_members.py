"""Offboarding: removing an org member revokes the API keys they own.

This is the security half of issue #160. Keys are minted with
``ttl_seconds=None`` and there was no column linking one to a person, so a
departing developer's credentials kept working indefinitely and no query could
even enumerate them.

The tests below pin four boundaries the sweep must respect — it keys on
``owner_user_id`` and joins through ``Project.org_id``, so it must not reach
another member's keys, another org's keys, or ownerless (system) keys — plus the
atomicity guarantee: a removal refused by the last-owner guard revokes nothing.

Fixtures mirror ``tests/features/tokens/test_tokens.py``.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import ROLE_MEMBER, ROLE_OWNER
from hexgate_api.core import keystore as keystore_mod
from hexgate_api.core.keystore import FileKeyStore
from hexgate_api.features.members.service import (
    LastOwnerError,
    find_member,
    remove_member,
)
from hexgate_api.features.orgs.service import create_org
from hexgate_api.features.projects.service import create_project
from hexgate_api.features.tokens.service import mint_api_key
from hexgate_api.main import app
from hexgate_api.models import ApiKey, OrganizationMember, User
from hexgate_api.seeds.defaults import ensure_default_project


@pytest_asyncio.fixture
async def session_factory():
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
async def signing_key(tmp_path) -> bytes:
    ks = FileKeyStore(base_dir=tmp_path / "keystore")
    ks.ensure_keypair()
    return ks._private_key_bytes()


@pytest_asyncio.fixture
async def client(session_factory, tmp_path) -> TestClient:
    from hexgate_api.core.db import get_session

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    original_keystore = keystore_mod.keystore
    keystore_mod.keystore = FileKeyStore(base_dir=tmp_path / "http-keystore")
    keystore_mod.keystore.ensure_keypair()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        keystore_mod.keystore = original_keystore


# ---------------------------------------------------------------------------
# Builders — a two-member org with a project, assembled through the real
# services so the actor columns are populated the way a route would.
# ---------------------------------------------------------------------------


async def _user(session: AsyncSession, email: str) -> User:
    user = User(email=email)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _org_with_two_members(
    session: AsyncSession, *, slug: str
) -> tuple[str, User, User]:
    """``(org_id, owner, member)`` — the owner keeps the org alive when the
    member is removed, so the last-owner guard doesn't mask what we're testing."""
    owner = await _user(session, f"owner-{slug}@example.com")
    member = await _user(session, f"member-{slug}@example.com")
    org = await create_org(
        session,
        name=slug,
        slug=slug,
        owner_user_id=owner.id,
        created_by_user_id=owner.id,
    )
    session.add(OrganizationMember(user_id=member.id, org_id=org.id, role=ROLE_MEMBER))
    await session.commit()
    return org.id, owner, member


async def _project(session: AsyncSession, *, org_id: str, name: str, actor: str) -> str:
    project = await create_project(
        session, org_id=org_id, name=name, created_by_user_id=actor
    )
    return project.id


async def _key(
    session: AsyncSession,
    *,
    project_id: str,
    name: str,
    signing_key: bytes,
    owner_user_id: str | None,
    created_by_user_id: str | None = None,
) -> ApiKey:
    row, _full = await mint_api_key(
        session,
        project_id,
        name,
        ["read_audit"],
        "live",
        signing_key_bytes=signing_key,
        created_by_user_id=created_by_user_id or owner_user_id,
        owner_user_id=owner_user_id,
    )
    return row


async def _reread(session_factory, token_id: str) -> ApiKey:
    async with session_factory() as session:
        row = await session.get(ApiKey, token_id)
        assert row is not None, "revocation is a soft delete; the row must survive"
        return row


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


async def test_remove_member_revokes_the_keys_they_own(
    session_factory, signing_key
) -> None:
    async with session_factory() as s:
        org_id, owner, member = await _org_with_two_members(s, slug="acme")
        pid = await _project(s, org_id=org_id, name="p1", actor=owner.id)
        key = await _key(
            s,
            project_id=pid,
            name="theirs",
            signing_key=signing_key,
            owner_user_id=member.id,
        )
        full_secret = key.secret

        result = await remove_member(
            s, org_id=org_id, user_id=member.id, removed_by_user_id=owner.id
        )

    assert result.removed is True
    assert result.revoked_key_count == 1

    row = await _reread(session_factory, key.id)
    assert row.revoked_at is not None
    # The remover is the actor, not the departing member.
    assert row.revoked_by_user_id == owner.id
    # Masked, so the retained audit row is not a usable credential.
    assert row.secret != full_secret


async def test_remove_member_revokes_keys_across_every_project_in_the_org(
    session_factory, signing_key
) -> None:
    """The sweep is org-wide. Membership is org-scoped but keys are
    project-scoped, so a project-scoped sweep would leave keys live in every
    project the code happened not to look at."""
    async with session_factory() as s:
        org_id, owner, member = await _org_with_two_members(s, slug="wide")
        first = await _project(s, org_id=org_id, name="p1", actor=owner.id)
        second = await _project(s, org_id=org_id, name="p2", actor=owner.id)
        key_a = await _key(
            s,
            project_id=first,
            name="a",
            signing_key=signing_key,
            owner_user_id=member.id,
        )
        key_b = await _key(
            s,
            project_id=second,
            name="b",
            signing_key=signing_key,
            owner_user_id=member.id,
        )

        result = await remove_member(
            s, org_id=org_id, user_id=member.id, removed_by_user_id=owner.id
        )

    assert result.revoked_key_count == 2
    assert (await _reread(session_factory, key_a.id)).revoked_at is not None
    assert (await _reread(session_factory, key_b.id)).revoked_at is not None


async def test_remove_member_leaves_another_members_keys_alone(
    session_factory, signing_key
) -> None:
    """The column is ``owner_user_id``, not "any key in the project"."""
    async with session_factory() as s:
        org_id, owner, member = await _org_with_two_members(s, slug="scoped")
        pid = await _project(s, org_id=org_id, name="p1", actor=owner.id)
        theirs = await _key(
            s,
            project_id=pid,
            name="theirs",
            signing_key=signing_key,
            owner_user_id=member.id,
        )
        mine = await _key(
            s,
            project_id=pid,
            name="mine",
            signing_key=signing_key,
            owner_user_id=owner.id,
        )

        result = await remove_member(
            s, org_id=org_id, user_id=member.id, removed_by_user_id=owner.id
        )

    assert result.revoked_key_count == 1
    assert (await _reread(session_factory, theirs.id)).revoked_at is not None
    assert (await _reread(session_factory, mine.id)).revoked_at is None


async def test_remove_member_leaves_keys_in_another_org_alone(
    session_factory, signing_key
) -> None:
    """Tenancy. The same person can own keys in two orgs; leaving one must not
    kill their credentials in the other."""
    async with session_factory() as s:
        org_id, owner, member = await _org_with_two_members(s, slug="tenant-a")
        other_org_id, other_owner, _ = await _org_with_two_members(s, slug="tenant-b")
        # The departing member also belongs to (and owns a key in) the other org.
        s.add(
            OrganizationMember(user_id=member.id, org_id=other_org_id, role=ROLE_MEMBER)
        )
        await s.commit()

        here = await _project(s, org_id=org_id, name="here", actor=owner.id)
        elsewhere = await _project(
            s, org_id=other_org_id, name="elsewhere", actor=other_owner.id
        )
        doomed = await _key(
            s,
            project_id=here,
            name="doomed",
            signing_key=signing_key,
            owner_user_id=member.id,
        )
        survivor = await _key(
            s,
            project_id=elsewhere,
            name="survivor",
            signing_key=signing_key,
            owner_user_id=member.id,
        )

        result = await remove_member(
            s, org_id=org_id, user_id=member.id, removed_by_user_id=owner.id
        )

    assert result.revoked_key_count == 1
    assert (await _reread(session_factory, doomed.id)).revoked_at is not None
    assert (await _reread(session_factory, survivor.id)).revoked_at is None


async def test_remove_member_leaves_ownerless_keys_alone(
    session_factory, signing_key
) -> None:
    """A NULL owner is a system mint (deploy/provision.py) or a key minted
    before the actor columns existed. Revoking those because an unrelated person
    left the org would be an outage, not an audit fix."""
    async with session_factory() as s:
        org_id, owner, member = await _org_with_two_members(s, slug="ownerless")
        pid = await _project(s, org_id=org_id, name="p1", actor=owner.id)
        system_key = await _key(
            s,
            project_id=pid,
            name="provisioned",
            signing_key=signing_key,
            owner_user_id=None,
        )

        result = await remove_member(
            s, org_id=org_id, user_id=member.id, removed_by_user_id=owner.id
        )

    assert result.removed is True
    assert result.revoked_key_count == 0
    assert (await _reread(session_factory, system_key.id)).revoked_at is None


async def test_remove_member_when_they_own_no_keys_then_count_is_zero(
    session_factory,
) -> None:
    async with session_factory() as s:
        org_id, owner, member = await _org_with_two_members(s, slug="nokeys")

        result = await remove_member(
            s, org_id=org_id, user_id=member.id, removed_by_user_id=owner.id
        )

    assert result.removed is True
    assert result.revoked_key_count == 0


async def test_remove_member_when_already_revoked_then_the_first_stamp_is_kept(
    session_factory, signing_key
) -> None:
    """The sweep goes through the same conditional UPDATE as a single revoke, so
    a key someone already revoked keeps its original actor and timestamp."""
    from hexgate_api.features.tokens.service import revoke_api_key

    async with session_factory() as s:
        org_id, owner, member = await _org_with_two_members(s, slug="tworevokes")
        pid = await _project(s, org_id=org_id, name="p1", actor=owner.id)
        key = await _key(
            s,
            project_id=pid,
            name="already-gone",
            signing_key=signing_key,
            owner_user_id=member.id,
        )
        # The member revokes it themselves first.
        assert await revoke_api_key(s, pid, key.id, revoked_by_user_id=member.id)

    first = await _reread(session_factory, key.id)

    async with session_factory() as s:
        result = await remove_member(
            s, org_id=org_id, user_id=member.id, removed_by_user_id=owner.id
        )

    # Nothing left to revoke, and the earlier audit fact is intact.
    assert result.revoked_key_count == 0
    after = await _reread(session_factory, key.id)
    assert after.revoked_at == first.revoked_at
    assert after.revoked_by_user_id == member.id


async def test_remove_member_when_refused_for_last_owner_then_no_key_is_revoked(
    session_factory, signing_key
) -> None:
    """A refused removal must leave the keys live.

    Otherwise this org's only owner keeps their access and loses their
    credentials — the worst of both outcomes.

    Note what actually holds this: ``revoke_owned_keys`` never commits, so an
    exception anywhere in ``remove_member`` discards the stamps with the
    transaction. Running the guard first is belt-and-braces on top. The
    load-bearing half is pinned separately by
    ``test_revoke_owned_keys_does_not_commit`` — reordering the guard alone does
    not break this test.
    """
    async with session_factory() as s:
        sole = await _user(s, "sole-owner@example.com")
        org = await create_org(
            s,
            name="solo",
            slug="solo",
            owner_user_id=sole.id,
            created_by_user_id=sole.id,
        )
        pid = await _project(s, org_id=org.id, name="p1", actor=sole.id)
        key = await _key(
            s,
            project_id=pid,
            name="still-needed",
            signing_key=signing_key,
            owner_user_id=sole.id,
        )

        with pytest.raises(LastOwnerError):
            await remove_member(
                s, org_id=org.id, user_id=sole.id, removed_by_user_id=sole.id
            )

    assert (await _reread(session_factory, key.id)).revoked_at is None
    async with session_factory() as s:
        assert await find_member(s, org_id=org.id, user_id=sole.id) is not None


async def test_revoke_owned_keys_does_not_commit(session_factory, signing_key) -> None:
    """The load-bearing half of the atomicity guarantee (decision D8).

    ``revoke_api_key`` commits; the sweep must not, so that a rollback anywhere
    in ``remove_member`` — a refused removal, a failing delete — takes the
    revocations with it. If someone "tidies up" by adding a commit here, a
    removal that 409s would still kill the member's keys.
    """
    from hexgate_api.features.tokens.service import revoke_owned_keys

    async with session_factory() as s:
        org_id, owner, member = await _org_with_two_members(s, slug="nocommit")
        pid = await _project(s, org_id=org_id, name="p1", actor=owner.id)
        key = await _key(
            s,
            project_id=pid,
            name="k",
            signing_key=signing_key,
            owner_user_id=member.id,
        )

        # Read the id out before the rollback: it detaches ``key``, and
        # touching an attribute afterwards raises DetachedInstanceError.
        key_id = key.id

        revoked = await revoke_owned_keys(
            s, org_id=org_id, owner_user_id=member.id, revoked_by_user_id=owner.id
        )
        assert revoked == 1  # the UPDATE ran...
        await s.rollback()  # ...and is undone, because it was never committed

    assert (await _reread(session_factory, key_id)).revoked_at is None


async def test_remove_member_commits_the_sweep_and_the_delete_together(
    session_factory, signing_key
) -> None:
    """One commit for both halves: the membership is gone AND the key is
    revoked when the call returns, with no window where only one landed."""
    async with session_factory() as s:
        org_id, owner, member = await _org_with_two_members(s, slug="atomic")
        pid = await _project(s, org_id=org_id, name="p1", actor=owner.id)
        key = await _key(
            s,
            project_id=pid,
            name="k",
            signing_key=signing_key,
            owner_user_id=member.id,
        )
        await remove_member(
            s, org_id=org_id, user_id=member.id, removed_by_user_id=owner.id
        )

    async with session_factory() as s:
        assert await find_member(s, org_id=org_id, user_id=member.id) is None
        rows = (
            await s.exec(select(ApiKey).where(ApiKey.owner_user_id == member.id))
        ).all()
        assert [r.id for r in rows] == [key.id]
        assert rows[0].revoked_at is not None


# ---------------------------------------------------------------------------
# Through the route — the "leave organization" flow, and the real bearer gate
# ---------------------------------------------------------------------------


def _signup(client: TestClient, email: str) -> str:
    r = client.post(
        "/v1/auth/register",
        json={"email": email, "password": "correcthorsebattery"},
    )
    assert r.status_code == 201, r.text
    _login(client, email)
    return client.get("/v1/users/me").json()["id"]


def _login(client: TestClient, email: str) -> None:
    r = client.post(
        "/v1/auth/cookie/login",
        data={"username": email, "password": "correcthorsebattery"},
    )
    assert r.status_code == 204, r.text


def test_leave_org_revokes_your_own_keys(client: TestClient) -> None:
    """A plain member removing themselves goes through
    ``require_org_admin_or_self``, and the sweep applies to them too."""
    owner_email, leaver_email = "stay@example.com", "leaving@example.com"
    _signup(client, owner_email)
    org_id = client.get("/v1/orgs").json()[0]["id"]
    pid = client.post(f"/v1/orgs/{org_id}/projects", json={"name": "shared"}).json()[
        "id"
    ]

    leaver_id = _signup(client, leaver_email)
    # Invite + accept so the membership is created the way the product does it.
    _login(client, owner_email)
    invite_id = client.post(
        f"/v1/orgs/{org_id}/invites",
        json={"email": leaver_email, "role": ROLE_MEMBER},
    ).json()["id"]
    _login(client, leaver_email)
    assert client.post(f"/v1/invites/{invite_id}/accept").status_code == 200

    minted = client.post(f"/v1/projects/{pid}/tokens", json={"name": "mine"}).json()
    headers = {"Authorization": f"Bearer {minted['full']}"}
    assert minted["owner_user_id"] == leaver_id
    assert client.get("/v1/me/key", headers=headers).status_code == 200

    assert client.delete(f"/v1/orgs/{org_id}/members/{leaver_id}").status_code == 204

    # The end-to-end proof: the key is rejected by the real revocation gate,
    # not merely stamped in a column.
    assert client.get("/v1/me/key", headers=headers).status_code == 401


def test_remove_member_route_still_404s_for_a_non_member(client: TestClient) -> None:
    """``MemberRemoval`` is an object, so a bare truthiness check in the route
    would turn this 404 into a 204."""
    _signup(client, "solo-route@example.com")
    org_id = client.get("/v1/orgs").json()[0]["id"]

    r = client.delete(f"/v1/orgs/{org_id}/members/00000000-0000-0000-0000-0000000000ff")
    assert r.status_code == 404, r.text


def test_remove_member_route_still_409s_for_the_last_owner(client: TestClient) -> None:
    owner_id = _signup(client, "lastowner-route@example.com")
    org_id = client.get("/v1/orgs").json()[0]["id"]

    r = client.delete(f"/v1/orgs/{org_id}/members/{owner_id}")
    assert r.status_code == 409, r.text


def test_change_member_role_stamps_the_caller(client: TestClient) -> None:
    """``PATCH`` role — the update half of the trail on organization_member."""
    admin_email, target_email = "roleadmin@example.com", "roletarget@example.com"
    admin_id = _signup(client, admin_email)
    org_id = client.get("/v1/orgs").json()[0]["id"]
    target_id = _signup(client, target_email)
    _login(client, admin_email)
    invite_id = client.post(
        f"/v1/orgs/{org_id}/invites",
        json={"email": target_email, "role": ROLE_MEMBER},
    ).json()["id"]
    _login(client, target_email)
    client.post(f"/v1/invites/{invite_id}/accept")
    _login(client, admin_email)

    r = client.patch(
        f"/v1/orgs/{org_id}/members/{target_id}", json={"role": ROLE_OWNER}
    )
    assert r.status_code == 200, r.text

    members = client.get(f"/v1/orgs/{org_id}/members").json()
    assert {m["user_id"] for m in members} == {admin_id, target_id}
