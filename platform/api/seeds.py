"""Seed content for a fresh project — mirrors asianf/examples/*.py policies."""

SEED_AGENTS = [
    {
        "name": "workspace_explorer",
        "agent_yaml": """name: workspace_explorer
model: gpt-5.4
system_prompt: system.md
tools:
  - glob
  - grep
  - read_file
policy: policy.yaml
""",
        "policy_yaml": """version: 1

default_policy:
  mode: deny

tools:
  glob:
    mode: allow
  grep:
    mode: allow
  read_file:
    mode: allow
""",
        "system_md": "You explore codebases and workspaces carefully.\n",
    },
    {
        "name": "update_researcher",
        "agent_yaml": """name: update_researcher
model: gpt-5.4
system_prompt: system.md
tools:
  - web_search
  - fetch
  - glob
  - grep
  - read_file
  - write_file
  - edit_file
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
  glob:
    mode: allow
  grep:
    mode: allow
  read_file:
    mode: allow
  write_file:
    mode: approval_required
    file_scope:
      allowed_paths:
        - "research_notes/*.md"
  edit_file:
    mode: approval_required
    file_scope:
      allowed_paths:
        - "research_notes/*.md"
""",
        "system_md": "You are an update researcher.\n",
    },
    {
        "name": "repo_operator",
        "agent_yaml": """name: repo_operator
model: gpt-5.4
system_prompt: system.md
tools:
  - glob
  - grep
  - read_file
  - write_file
  - edit_file
  - bash
policy: policy.yaml
""",
        "policy_yaml": """version: 1

default_policy:
  mode: deny

tools:
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
        "system_md": "You are a careful coding assistant.\n",
    },
]
