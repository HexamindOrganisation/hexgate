"""DevOps (3-level) deploy showcase — seed resolution.

Mirrors the compose support-bot tests, three levels deep: the seeded
``devops_seed`` policy must resolve a working developer → release_lead →
deploy_bot chain (reach gated at each hop, the privileged ops capped per role on
the leaf, prod version deploys approval-gated). The notebook↔seed drift guard
lives in test_notebook_drift.py (parametrized over every showcase demo).
"""

from __future__ import annotations


async def test_seeded_devops_resolves(session_factory) -> None:
    # The showcase seed must be a real, resolvable multi-agent compose policy: a
    # 3-level deploy chain where each tier reaches the next and only the leaf holds
    # the privileged restart/deploy ops.
    from hexgate_api.constants import DEFAULT_PROJECT_ID
    from hexgate_api.features.policy_modules import service as svc
    from hexgate_api.features.policy_modules.devops_seed import (
        DEVOPS_SEED_FILES,
        ensure_seeded_devops_policy,
    )

    async with session_factory() as s:
        await ensure_seeded_devops_policy(s, DEFAULT_PROJECT_ID)
        names = {f.name for f in await svc.list_files(s, DEFAULT_PROJECT_ID)}
        assert set(DEVOPS_SEED_FILES) <= names

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
