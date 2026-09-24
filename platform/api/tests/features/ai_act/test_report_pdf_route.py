"""GET /v1/projects/{id}/ai-act/reports/{rpt_id}.pdf.

The route is thin — look the report up, render its stored annex, hand back the
bytes — so what these tests hold is the thin part: who may fetch it, what an
unknown id does, and that a render failure comes back as a 502 carrying a
diagnostic rather than as a truncated PDF body.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.constants import ROLE_MEMBER
from hexgate_api.core import keystore as keystore_mod
from hexgate_api.main import app
from hexgate_api.models import AiActReport, OrganizationMember, User
from hexgate_api.seeds.defaults import ensure_default_project
from tests.features.ai_act.annex_fixture import sample_annex

REPORT_ID = "rpt_000000000000"


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
async def client(session_factory, tmp_path) -> TestClient:
    from hexgate_api.core.db import get_session
    from hexgate_api.core.keystore import FileKeyStore

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    original_keystore = keystore_mod.keystore
    keystore_mod.keystore = FileKeyStore(base_dir=tmp_path / "keystore")
    keystore_mod.keystore.ensure_keypair()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        keystore_mod.keystore = original_keystore


def _login(client: TestClient, email: str) -> None:
    r = client.post(
        "/v1/auth/register", json={"email": email, "password": "correcthorsebattery"}
    )
    assert r.status_code == 201, r.text
    r = client.post(
        "/v1/auth/cookie/login",
        data={"username": email, "password": "correcthorsebattery"},
    )
    assert r.status_code == 204, r.text


def _make_project(client: TestClient, *, email: str, name: str = "proj") -> str:
    _login(client, email)
    org_id = client.get("/v1/orgs").json()[0]["id"]
    r = client.post(f"/v1/orgs/{org_id}/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _insert_report(session_factory, *, project_id: str) -> None:
    annex_json = json.dumps(sample_annex())
    async with session_factory() as session:
        session.add(
            AiActReport(
                id=REPORT_ID,
                project_id=project_id,
                period_start=datetime(2026, 3, 21, tzinfo=timezone.utc),
                period_end=datetime(2026, 9, 17, tzinfo=timezone.utc),
                generated_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
                generated_by_user_id="usr_1",
                annex_json=annex_json,
                annex_sha256="0" * 64,
                annex_bytes=len(annex_json.encode("utf-8")),
                signature=b"\x00" * 64,
                signing_kid="sha256:0123456789abcdef",
            )
        )
        await session.commit()


def _store_report(session_factory, *, project_id: str) -> None:
    """The stored report the route reads. Sync, so the TestClient tests that
    need one can call it inline."""
    asyncio.run(_insert_report(session_factory, project_id=project_id))


def test_report_pdf_happy_path(client: TestClient, session_factory) -> None:
    project_id = _make_project(client, email="owner@example.com")
    _store_report(session_factory, project_id=project_id)

    r = client.get(f"/v1/projects/{project_id}/ai-act/reports/{REPORT_ID}.pdf")

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == f'attachment; filename="{REPORT_ID}.pdf"'
    assert r.content.startswith(b"%PDF-")


def test_when_the_report_id_is_unknown_then_404(client: TestClient) -> None:
    project_id = _make_project(client, email="owner@example.com")

    r = client.get(f"/v1/projects/{project_id}/ai-act/reports/rpt_nope.pdf")

    assert r.status_code == 404, r.text


async def _add_org_member(
    session_factory, *, email: str, org_id: str, role: str
) -> str:
    """A user with a real membership in the project's org, at ``role``."""
    async with session_factory() as s:
        user = User(email=email)
        s.add(user)
        await s.commit()
        await s.refresh(user)
        s.add(
            OrganizationMember(
                id=str(uuid.uuid4()), user_id=user.id, org_id=org_id, role=role
            )
        )
        await s.commit()
        return user.id


def test_when_the_caller_is_a_plain_org_member_then_the_pdf_is_refused(
    client: TestClient, session_factory
) -> None:
    """The in-between case, and the one the route exists to refuse.

    Section 3.4 of the document prints the annex's ban-enforcement rows — who
    was blocked and the operator's free-text reason — which every other route
    serving that data admin-gates. A non-member 403 does not cover this: the
    gate was ``require_org_member`` and the suite was green, because the only
    other callers tested were the org owner, who passes either check, and an
    outsider, who fails both. ``.pdf`` was the way around the boundary.
    """
    project_id = _make_project(client, email="owner@example.com")
    _store_report(session_factory, project_id=project_id)
    org_id = client.get("/v1/orgs").json()[0]["id"]
    member = asyncio.run(
        _add_org_member(
            session_factory, email="member@example.com", org_id=org_id, role=ROLE_MEMBER
        )
    )
    client.cookies.clear()

    r = client.get(
        f"/v1/projects/{project_id}/ai-act/reports/{REPORT_ID}.pdf",
        headers={"X-Dev-User": member},
    )

    assert r.status_code == 403, r.text


def test_when_the_caller_is_not_an_org_member_then_the_pdf_is_refused(
    client: TestClient,
) -> None:
    project_id = _make_project(client, email="owner@example.com")
    # A second signup lands in its own personal org, with no claim on the first.
    _login(client, "outsider@example.com")

    r = client.get(f"/v1/projects/{project_id}/ai-act/reports/{REPORT_ID}.pdf")

    assert r.status_code == 403, r.text


def test_when_the_caller_is_anonymous_then_the_pdf_is_refused(
    client: TestClient,
) -> None:
    r = client.get(f"/v1/projects/prj_nope/ai-act/reports/{REPORT_ID}.pdf")

    assert r.status_code == 401, r.text


def test_when_the_report_id_carries_header_syntax_then_it_never_reaches_a_header(
    client: TestClient,
) -> None:
    """The download filename comes from the stored row, so a path parameter
    carrying a quote is a lookup miss and nothing else."""
    project_id = _make_project(client, email="owner@example.com")

    r = client.get(f'/v1/projects/{project_id}/ai-act/reports/rpt_a";x=y.pdf')

    assert r.status_code == 404, r.text
    assert "x=y" not in r.headers.get("content-disposition", "")


def test_when_the_render_fails_then_502_carries_the_diagnostics(
    client: TestClient, session_factory, monkeypatch
) -> None:
    project_id = _make_project(client, email="owner@example.com")
    _store_report(session_factory, project_id=project_id)

    def boom(*_args, **_kwargs):
        raise OSError("pango is not installed")

    monkeypatch.setattr("weasyprint.HTML", boom)

    r = client.get(f"/v1/projects/{project_id}/ai-act/reports/{REPORT_ID}.pdf")

    assert r.status_code == 502, r.text
    detail = r.json()["detail"]
    assert "pango is not installed" in detail["renderer"]
    assert not r.content.startswith(b"%PDF-")


@pytest.mark.parametrize("suffix", [".pdf", "/annex"])
def test_the_pdf_and_the_annex_answer_on_the_same_report(
    client: TestClient, session_factory, suffix: str
) -> None:
    """Both are downloads of one stored report; the annex is what the
    signature covers and the PDF is a rendering of it."""
    project_id = _make_project(client, email="owner@example.com")
    _store_report(session_factory, project_id=project_id)

    r = client.get(f"/v1/projects/{project_id}/ai-act/reports/{REPORT_ID}{suffix}")

    assert r.status_code == 200, r.text
