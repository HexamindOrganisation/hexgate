"""Seed content for a fresh project.

The `default` agent showcases the full tool surface of the SDK —
web search, web fetch, filesystem navigation, file editing, and bash.
Risk gradient is encoded in the policy: read-only tools are `allow`,
side-effecting tools are `approval_required`. This gives the /graph
canvas meaningful edge colour variation out of the box.

`read_only` is the foil: same toolset, everything mutating is `deny`.
Two agents, one permissive + gated, one strictly read-only — enough
to tell the story of policy-driven control in a demo.
"""

DEFAULT_AGENT_NAME = "default"

_ALL_TOOLS_YAML = """
  - web_search
  - fetch
  - glob
  - grep
  - read_file
  - write_file
  - edit_file
  - bash
"""


SEED_AGENTS = [
    {
        "name": DEFAULT_AGENT_NAME,
        "agent_yaml": f"""name: default
model: gpt-5.4
system_prompt: system.md
tools:{_ALL_TOOLS_YAML}policy: policy.yaml
""",
        "policy_yaml": """version: 1

default_policy:
  mode: deny

tools:
  web_search:
    mode: allow
  fetch:
    mode: allow
  glob:
    mode: allow
  grep:
    mode: allow
  read_file:
    mode: allow
  write_file:
    mode: approval_required
  edit_file:
    mode: approval_required
  bash:
    mode: approval_required
""",
        "system_md": (
            "You are the project's default agent with broad access to search, "
            "fetch, inspect a workspace, and run commands. Reads run freely; "
            "writes and shell commands require approval.\n"
        ),
    },
    {
        "name": "read_only",
        "agent_yaml": f"""name: read_only
model: gpt-5.4
system_prompt: system.md
tools:{_ALL_TOOLS_YAML}policy: policy.yaml
""",
        "policy_yaml": """version: 1

default_policy:
  mode: deny

tools:
  web_search:
    mode: allow
  fetch:
    mode: deny
  glob:
    mode: allow
  grep:
    mode: allow
  read_file:
    mode: allow
  write_file:
    mode: deny
  edit_file:
    mode: deny
  bash:
    mode: deny
""",
        "system_md": (
            "You only read. No fetches, no writes, no shell — if a request "
            "needs any of those, explain why you cannot and stop.\n"
        ),
    },
    # ----- support-bot: role-aware demo agent -------------------------------
    # Same agent, three role policies. The Playground's "Acting as" dropdown
    # exercises the role-driven policy selection added in phase 4a; the
    # `refund_order` tool's constraints fire differently per role to make
    # the demo visible (allow with cap → allow with bigger cap → deny).
    {
        "name": "support_bot",
        "agent_yaml": """name: support_bot
model: gpt-5.4
system_prompt: system.md
tools:
  - web_search
  - read_file
  - refund_order
policy: policy.yaml
""",
        # Legacy single-policy field — kept as the deny-all default so an
        # SDK call without a role still has somewhere to land.
        "policy_yaml": """version: 1

default_policy:
  mode: deny

tools:
  web_search: { mode: allow }
  read_file:  { mode: allow }
  refund_order: { mode: deny }
""",
        "system_md": (
            "You are a support assistant. You can look up information, read "
            "internal docs, and — if your role permits — issue refunds for "
            "customer orders. Always confirm the amount and customer id "
            "before calling refund_order.\n"
        ),
        # New role-aware bundle. Mixin `read_only` shares the safe base; the
        # two concrete roles add `refund_order` with different caps via
        # constraints. `default` is the deny-everything fallback (mirrors
        # `policy_yaml` above).
        "roles": {
            "read_only": """version: 1
is_mixin: true
default_policy:
  mode: deny
tools:
  web_search: { mode: allow }
  read_file:  { mode: allow }
""",
            "default": """version: 1
inherits: [read_only]
tools:
  refund_order:
    mode: deny
""",
            "support": """version: 1
inherits: [read_only]
tools:
  refund_order:
    mode: allow
    constraints:
      - args.amount <= 50
      - args.currency == "USD"
""",
            "billing": """version: 1
inherits: [read_only]
tools:
  refund_order:
    mode: allow
    constraints:
      - args.amount <= 500
      - args.currency in ["USD", "EUR"]
""",
        },
    },
]
