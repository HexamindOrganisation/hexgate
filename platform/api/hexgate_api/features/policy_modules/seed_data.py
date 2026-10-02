"""First-boot seed for the compose policy showcase.

Writes the demo's `policy.yaml` + capability files into the default project's
`policy_file` store, so the dashboard's **Policies** editor opens on a real
multi-module compose policy — a front-line ``support_bot`` with role→agent
admission (who may start it) and tool permissions composed from imported
capabilities. support_bot has no refund tool: no seat refunds directly, so every
refund goes through the ``billing_bot`` sub-agent, reached as an agent-as-tool and
gated by the reach edge ``agent.tool:billing_bot`` (the user has no direct access).
``billing_bot`` is its OWN compose agent here — registered and bound at serve — so
its per-role refund caps ($200 support / $1000 billing) are dashboard-editable too.
The same scenario runs locally in ``deploy/compose_support_demo.py``.

Direct row inserts (not the write-time flip/recompile path), so seeding is a
pure store fixture — idempotent per ``(project_id, name)``.
"""

from __future__ import annotations

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from hexgate_api.core.ids import new_id
from hexgate_api.features.policy_modules.service import _content_hash
from hexgate_api.models import PolicyFile

# The entry file: a closed-world boundary + support_bot, whose roles import the
# capability files below (organized into caps/base, caps/support, caps/billing).
# Kept textually in sync with the marimo demo.
_ENTRY = """\
boundary:
  tools:
    view_orders: { mode: allow }
    send_email: { mode: allow }
    escalate: { mode: allow }
    refund_order: { mode: allow, constraint: "args.amount <= 1000" }  # global org cap — declared, but granted to NO support_bot seat: refunds happen only inside billing_bot
    mcp-demo-compute_tip: { mode: allow }     # safe MCP tool
    mcp-demo-send_invoice: { mode: allow }    # ceiling; billing grants w/ approval
    mcp-demo-read_secret: { mode: deny }      # dangerous MCP tool — always denied
  reach:
    billing_bot: { as: tool }   # reach ceiling: support_bot may be granted agent-as-tool reach to billing_bot
  admission: { mode: allow }    # ingress ceiling: a seat may be admitted to start / be delegated to a bot
agents:
  # Front-line agent. Has NO refund_order tool — refunds happen only inside
  # billing_bot, reached as an agent-as-tool. The delegation is gated by the
  # REACH key agent.tool:billing_bot (not a plain tool name), so support_bot's
  # policy governs the sub-agent tool use per role.
  support_bot:
    roles:
      # The default seat browses read-only data but may NOT start the bot (no ingress).
      default: { import: [ caps/base/read_only.yaml ] }
      # The support seat starts the front-line bot and reaches billing_bot as a
      # tool (support_bot has no refund_order tool — nobody refunds direct).
      support:
        import:
          [ caps/base/read_only.yaml, caps/base/ingress.yaml,
            caps/support/desk.yaml, caps/support/delegate.yaml ]
      # The billing seat additionally may queue invoices (approval); it still
      # refunds only by delegating — billing_bot caps its delegation higher.
      billing:
        import:
          [ caps/base/read_only.yaml, caps/base/ingress.yaml,
            caps/support/desk.yaml, caps/billing/invoicing.yaml,
            caps/support/delegate.yaml ]
  # The billing specialist, reached only as support_bot's agent-as-tool (never
  # started directly — no seat has direct access). It is its OWN compose agent, so
  # the platform binds this policy to it at serve and it's dashboard-editable. Its
  # refund cap rides the delegating seat's role into the nested run: support up to
  # $200, billing up to the $1000 org ceiling, any other seat nothing.
  billing_bot:
    roles:
      # Not admitted and grants no refund — a delegated default seat bills nothing
      # (support_bot already denies default the reach; this is defense in depth).
      default: { import: [ caps/base/read_only.yaml ] }
      # A support delegation is admitted and refunds up to $200.
      support:
        import: [ caps/base/ingress.yaml, caps/billing/refund_support.yaml ]
      # A billing delegation is admitted and refunds up to the $1000 org ceiling.
      billing:
        import: [ caps/base/ingress.yaml, caps/billing/refund_billing.yaml ]
"""

# Leaf capability files (grant-only), imported by the roles above.
_CAPS = {
    "caps/base/read_only.yaml": (
        "tools:\n  view_orders: { mode: allow }\n"
        "mcp:\n  mcp-demo-compute_tip: { mode: allow }\n"
    ),
    # The ingress grant: a role that imports this may be admitted to START (or be
    # delegated to) the agent it's imported into (lowers to the agent.run key).
    "caps/base/ingress.yaml": ("admission:\n  mode: allow\n"),
    "caps/support/desk.yaml": (
        "tools:\n"
        "  send_email: { mode: allow }\n"
        "  escalate: { mode: approval_required }\n"
    ),
    # support_bot reaches billing_bot as an agent-as-tool: a reach grant (not the
    # delegate_to_billing tool name), so the delegation is gated as a reach edge
    # (lowered to agent.tool:billing_bot) under support_bot's policy per role.
    "caps/support/delegate.yaml": ("reach:\n  billing_bot: { as: tool }\n"),
    "caps/billing/invoicing.yaml": (
        "mcp:\n  mcp-demo-send_invoice: { mode: approval_required }\n"
    ),
    # billing_bot's refund grants (imported by its support/billing roles). Each cap
    # intersects with the boundary's $1000 org ceiling, so support lands at $200.
    "caps/billing/refund_support.yaml": (
        'tools:\n  refund_order: { mode: allow, constraint: "args.amount <= 200" }\n'
    ),
    "caps/billing/refund_billing.yaml": (
        'tools:\n  refund_order: { mode: allow, constraint: "args.amount <= 1000" }\n'
    ),
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
