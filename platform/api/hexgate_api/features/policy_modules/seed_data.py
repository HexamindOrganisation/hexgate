"""First-boot seed for the compose policy showcase.

Writes the demo's `policy.yaml` + capability files into the default project's
`policy_file` store, so the dashboard's **Policies** editor opens on a real
multi-module compose policy — a front-line ``support_bot`` and a refunds
specialist ``billing_bot``, tool permissions and agent reach composed from the
same imported capabilities. The same scenario runs locally in
``deploy/compose_support_demo.py``.

Direct row inserts (not the write-time flip/recompile path), so seeding is a
pure store fixture — idempotent per ``(project_id, name)``.
"""

from __future__ import annotations

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.core.ids import new_id
from hexgate_api.features.policy_modules.service import _content_hash
from hexgate_api.models import PolicyFile

# The entry file: a closed-world boundary + two agents whose roles import the
# capability files below. Kept textually in sync with the marimo demo.
_ENTRY = """\
boundary:
  tools:
    view_orders: { mode: allow }
    send_email: { mode: allow }
    escalate: { mode: allow }
    refund_order: { mode: allow, constraint: "args.amount <= 1000" }  # hard cap
    delegate_to_billing: { mode: allow }   # ceiling; needs a capability grant
  reach:
    billing_bot: { as: handoff }   # reach ceiling: hand-off only, never as-tool
agents:
  support_bot:
    roles:
      default: { import: [ caps/read_only.yaml ] }
      support: { import: [ caps/read_only.yaml, caps/support_leaf.yaml ] }
      billing:
        import:
          [ caps/read_only.yaml, caps/payments.yaml, caps/billing_desk.yaml,
            caps/billing_reach.yaml ]
  billing_bot:
    roles:
      billing: { import: [ caps/payments.yaml ] }
"""

# Leaf capability files (grant-only), imported by the roles above.
_CAPS = {
    "caps/read_only.yaml": "tools:\n  view_orders: { mode: allow }\n",
    "caps/support_leaf.yaml": (
        "tools:\n"
        "  send_email: { mode: allow }\n"
        "  escalate: { mode: approval_required }\n"
    ),
    "caps/payments.yaml": (
        "tools:\n"
        "  refund_order: { mode: allow, constraint: "
        '\'args.currency in ["USD", "EUR"]\' }\n'
    ),
    # Grants the delegate-to-billing TOOL (a served sub-agent surfaces delegation
    # as a plain tool, so it's gated as one) — only the billing role imports it.
    "caps/billing_desk.yaml": "tools:\n  delegate_to_billing: { mode: allow }\n",
    "caps/billing_reach.yaml": "reach:\n  billing_bot: { as: handoff }\n",
}

SEED_POLICY_FILES: dict[str, str] = {"policy.yaml": _ENTRY, **_CAPS}


async def ensure_seeded_compose_policy(session: AsyncSession, project_id: str) -> None:
    """Idempotently seed the demo compose policy files for ``project_id``.

    Only inserts a file that isn't already present, so re-seeding an existing DB
    (or one an operator has since edited) never clobbers their content.
    """
    existing = set(
        (
            await session.exec(
                select(PolicyFile.name).where(PolicyFile.project_id == project_id)
            )
        ).all()
    )
    added = False
    for name, content in SEED_POLICY_FILES.items():
        if name in existing:
            continue
        session.add(
            PolicyFile(
                id=new_id(PolicyFile),
                project_id=project_id,
                name=name,
                content=content,
                content_hash=_content_hash(content),
            )
        )
        added = True
    if added:
        await session.commit()
