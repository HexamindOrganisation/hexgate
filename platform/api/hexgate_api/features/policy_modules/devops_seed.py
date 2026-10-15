"""First-boot seed for the DevOps (3-level) delegation showcase.

Writes a `policy.yaml` + capability files describing a **three-level deploy
chain** — `developer` → `release_lead` → `deploy_bot` — into a project's
`policy_file` store, so the dashboard's **Policies** editor opens on it and a
served `developer` tree is gated by it.

The point: the privileged ops (`restart_service`, `deploy_version`) live only on
the deepest agent (`deploy_bot`), and each tier must *delegate down* to reach
them — `developer` reaches `release_lead` (`agent.tool:release_lead`),
`release_lead` reaches `deploy_bot` (`agent.tool:deploy_bot`). Every hop is gated
under the *calling* agent's policy, the caller's role rides the ambient context
down all three levels, and the stage/approval caps fire at the leaf:

  * `viewer`   — reads service status via `developer`, but can't escalate at all.
  * `engineer` — escalates the chain; restarts/deploys **staging** only.
  * `release`  — escalates the chain; restarts any env, and **version deploys need
    approval** (change control), prod included.

All three are their own compose agents — registered and bound at serve — so each
tier's policy is dashboard-editable. The same scenario runs locally in
``deploy/devops_demo.py``.

Direct row inserts (not the write-time flip/recompile path), so seeding is a
pure store fixture — idempotent per ``(project_id, name)``.
"""

from __future__ import annotations

from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.features.policy_modules.service import ensure_seeded_policy_files

# The entry file: a closed-world boundary + the three deploy-chain agents, whose
# roles import the capability files below. Kept textually in sync with the marimo
# demo (deploy/devops_demo.py, _FILES) — the drift test asserts byte-equality.
_ENTRY = """\
boundary:
  tools:
    service_status: { mode: allow }    # safe read — any admitted seat
    restart_service: { mode: allow }   # privileged; granted only on deploy_bot, capped per role
    deploy_version: { mode: allow }    # privileged; granted only on deploy_bot (prod = change control)
  reach:
    release_lead: { as: tool }   # reach ceiling: developer may be granted agent-as-tool reach to release_lead
    deploy_bot: { as: tool }     # reach ceiling: release_lead may be granted agent-as-tool reach to deploy_bot
  admission: { mode: allow }    # ingress ceiling: a seat may be admitted to start / be delegated to an agent
agents:
  # L1 — the only agent the user talks to. Reads service status; it has NO deploy
  # tool, so a restart/deploy must escalate to release_lead (agent.tool:release_lead).
  developer:
    roles:
      # viewer may start developer and read status but NOT escalate (no reach) —
      # a deploy request dies here, at the first hop.
      viewer:
        import: [ caps/base/ingress.yaml, caps/base/status.yaml ]
      # engineer/release may start developer and escalate down the chain.
      engineer:
        import: [ caps/base/ingress.yaml, caps/base/status.yaml, caps/dev/escalate.yaml ]
      release:
        import: [ caps/base/ingress.yaml, caps/base/status.yaml, caps/dev/escalate.yaml ]
  # L2 — the release lead. Reached only as developer's agent-as-tool; its only move
  # is to escalate further to deploy_bot (reach edge agent.tool:deploy_bot).
  release_lead:
    roles:
      # Never reached by viewer (developer denies it the reach) and not admitted
      # here either — defense in depth.
      viewer: { import: [ caps/base/status.yaml ] }
      engineer:
        import: [ caps/base/ingress.yaml, caps/release/escalate.yaml ]
      release:
        import: [ caps/base/ingress.yaml, caps/release/escalate.yaml ]
  # L3 — the deploy bot that actually holds restart_service + deploy_version, three
  # hops from the user and reached only as release_lead's agent-as-tool. The caps
  # ride the delegating seat's role: engineer may act on STAGING only; release may
  # restart any env, and its version deploys are approval-gated (change control).
  deploy_bot:
    roles:
      viewer: { import: [ caps/base/status.yaml ] }
      engineer:
        import: [ caps/base/ingress.yaml, caps/deploy/staging.yaml ]
      release:
        import: [ caps/base/ingress.yaml, caps/deploy/prod.yaml ]
"""

# Leaf capability files (grant-only), imported by the roles above.
_CAPS = {
    "caps/base/status.yaml": ("tools:\n  service_status: { mode: allow }\n"),
    # The ingress grant: a role that imports this may be admitted to START (or be
    # delegated to) the agent it's imported into (lowers to the agent.run key).
    "caps/base/ingress.yaml": ("admission:\n  mode: allow\n"),
    # developer reaches release_lead as an agent-as-tool (lowered to
    # agent.tool:release_lead), gated under developer's policy per role.
    "caps/dev/escalate.yaml": ("reach:\n  release_lead: { as: tool }\n"),
    # release_lead reaches deploy_bot the same way (agent.tool:deploy_bot).
    "caps/release/escalate.yaml": ("reach:\n  deploy_bot: { as: tool }\n"),
    # engineer: restart + deploy STAGING only (the env arg is constrained).
    "caps/deploy/staging.yaml": (
        "tools:\n"
        "  restart_service: { mode: allow, constraint: 'args.env == \"staging\"' }\n"
        "  deploy_version: { mode: allow, constraint: 'args.env == \"staging\"' }\n"
    ),
    # release: restart ANY env freely; a version deploy to ANY env needs approval
    # (change control) — the deploy prompt the Playground surfaces.
    "caps/deploy/prod.yaml": (
        "tools:\n"
        "  restart_service: { mode: allow }\n"
        "  deploy_version: { mode: approval_required }\n"
    ),
}

DEVOPS_SEED_FILES: dict[str, str] = {"policy.yaml": _ENTRY, **_CAPS}


async def ensure_seeded_devops_policy(session: AsyncSession, project_id: str) -> None:
    """Idempotently seed the DevOps compose policy files for ``project_id``."""
    await ensure_seeded_policy_files(session, project_id, DEVOPS_SEED_FILES)
