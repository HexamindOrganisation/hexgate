"""DevOps (3-level) deploy showcase — seed resolution + notebook drift.

Mirrors the compose support-bot tests, three levels deep: the seeded
``devops_seed`` policy must resolve a working developer → release_lead →
deploy_bot chain (reach gated at each hop, the privileged ops capped per role on
the leaf, prod version deploys approval-gated), and the marimo notebook's inlined
``_FILES`` must stay byte-identical to the seed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.seeds.defaults import ensure_default_project


@pytest_asyncio.fixture
async def session_factory():
    """In-memory async SQLite + factory, seeded with the triple-default (mirrors
    the fixture in test_policy_modules.py)."""
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


async def test_seeded_devops_resolves(session_factory) -> None:
    # The showcase seed must be a real, resolvable multi-agent compose policy: a
    # 3-level deploy chain where each tier reaches the next and only the leaf holds
    # the privileged restart/deploy ops.
    from hexgate_api.constants import DEFAULT_PROJECT_ID
    from hexgate_api.features.policy_modules import service as svc
    from hexgate_api.features.policy_modules.devops_seed import (
        ensure_seeded_devops_policy,
    )

    async with session_factory() as s:
        await ensure_seeded_devops_policy(s, DEFAULT_PROJECT_ID)
        names = {f.name for f in await svc.list_files(s, DEFAULT_PROJECT_ID)}
        assert {
            "policy.yaml",
            "caps/base/status.yaml",
            "caps/base/ingress.yaml",
            "caps/dev/escalate.yaml",
            "caps/release/escalate.yaml",
            "caps/deploy/staging.yaml",
            "caps/deploy/prod.yaml",
        } <= names

        dev = (
            await svc.compose_resolve(s, DEFAULT_PROJECT_ID, agent="developer")
        ).policy_set
        rl = (
            await svc.compose_resolve(s, DEFAULT_PROJECT_ID, agent="release_lead")
        ).policy_set
        db = (
            await svc.compose_resolve(s, DEFAULT_PROJECT_ID, agent="deploy_bot")
        ).policy_set

        def m(ps, role, tool, **args):
            return ps.evaluate(role=role, tool=tool, args=args).outcome.value

        # L1 developer: every seat may start it and read status; the reach to
        # release_lead is granted to engineer/release and DENIED to viewer.
        for r in ("viewer", "engineer", "release"):
            assert m(dev, r, "agent.run") == "allow"
            assert m(dev, r, "service_status") == "allow"
        assert m(dev, "viewer", "agent.tool:release_lead") == "deny"
        assert m(dev, "engineer", "agent.tool:release_lead") == "allow"
        assert m(dev, "release", "agent.tool:release_lead") == "allow"

        # L2 release_lead: engineer/release are admitted and may reach deploy_bot;
        # viewer is neither (defense in depth).
        assert m(rl, "viewer", "agent.run") == "deny"
        assert m(rl, "engineer", "agent.run") == "allow"
        assert m(rl, "engineer", "agent.tool:deploy_bot") == "allow"
        assert m(rl, "release", "agent.tool:deploy_bot") == "allow"
        assert m(rl, "viewer", "agent.tool:deploy_bot") == "deny"

        # L3 deploy_bot: the privileged ops, capped by the caller's role.
        assert m(db, "viewer", "agent.run") == "deny"
        # engineer — staging only, no prod.
        assert m(db, "engineer", "restart_service", env="staging") == "allow"
        assert m(db, "engineer", "restart_service", env="prod") == "deny"
        assert m(db, "engineer", "deploy_version", env="staging") == "allow"
        assert m(db, "engineer", "deploy_version", env="prod") == "deny"
        # release — restart any env; a version deploy to any env needs approval.
        assert m(db, "release", "restart_service", env="staging") == "allow"
        assert m(db, "release", "restart_service", env="prod") == "allow"
        assert m(db, "release", "deploy_version", env="staging") == "needs_approval"
        assert m(db, "release", "deploy_version", env="prod") == "needs_approval"


def _find_devops_notebook() -> Path | None:
    """Locate deploy/devops_demo.py from the test file, or None."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "deploy" / "devops_demo.py"
        if candidate.exists():
            return candidate
    return None


def _notebook_dict(src: str, name: str) -> dict:
    """Return the literal dict assigned to ``name`` in the notebook source, parsed
    with ``ast`` so a cosmetic reformat can't break the drift guard."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else []
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"could not locate {name} in the demo notebook")


def test_notebook_devops_files_match_seed() -> None:
    # The marimo demo inlines the same compose policy the API seeds, kept aligned by
    # hand. Assert the two copies are byte-identical so an edit to one can't silently
    # drift from the other.
    from hexgate_api.features.policy_modules.devops_seed import DEVOPS_SEED_FILES

    notebook = _find_devops_notebook()
    if notebook is None:
        pytest.skip("devops demo notebook not present in this checkout")

    src = notebook.read_text(encoding="utf-8")
    assert _notebook_dict(src, "_FILES") == DEVOPS_SEED_FILES
