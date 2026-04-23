"""Seed content for a fresh project.

Every project gets at least a `default` agent on creation — this is the
fallback `load_agent()` resolves to when no name is passed and no
`FORTIFY_AGENT_NAME` env var is set. The `read_only` companion is included
so the graph has meaningful edge-mode variation out of the box.

Only tools that resolve from YAML via coolagents' builtin registry are
used here (`web_search`, `fetch`). File-oriented tools remain for
code-defined agents until the registry is extended.
"""

DEFAULT_AGENT_NAME = "default"

SEED_AGENTS = [
    {
        "name": DEFAULT_AGENT_NAME,
        "agent_yaml": """name: default
model: gpt-5.4
system_prompt: system.md
tools:
  - web_search
  - fetch
policy: policy.yaml
""",
        "policy_yaml": """version: 1

default_policy:
  mode: deny

tools:
  web_search:
    mode: allow
  fetch:
    mode: allow
""",
        "system_md": "You are the project's default agent with full access to all allowed tools.\n",
    },
    {
        "name": "read_only",
        "agent_yaml": """name: read_only
model: gpt-5.4
system_prompt: system.md
tools:
  - web_search
  - fetch
policy: policy.yaml
""",
        "policy_yaml": """version: 1

default_policy:
  mode: deny

tools:
  web_search:
    mode: allow
  fetch:
    mode: deny
""",
        "system_md": "You browse search results only — fetching pages is denied.\n",
    },
]
