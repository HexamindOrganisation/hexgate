"""Demo: fetch an agent's policy from the Fortify control plane.

Usage:
    # 1. Start backend:  cd platform/api && uv run uvicorn main:app --port 8000
    # 2. Mint a dev token at http://localhost:5173/tokens
    # 3. Put it in asianf/.env:
    #        FORTIFY_KEY=fty_test_support-bot_...
    # 4. Run from the asianf/ directory:
    #        python examples/fortify_agent_demo.py
    #        python examples/fortify_agent_demo.py read_only   # override name
    #
    # With only FORTIFY_KEY set, the SDK fetches the project's `default`
    # agent. Set FORTIFY_AGENT_NAME to override, or pass an arg at the CLI.
    #
    # Then open http://localhost:5173/agents, flip a tool's mode in the YAML,
    # hit Save, re-run this script — the printed policy updates.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Load asianf/.env before importing anything that reads FORTIFY_KEY.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fortify import AgentPolicy, FortifyClient, FortifyConfig  # noqa: E402
from fortify.cloud.client import FortifyError, resolve_agent_name  # noqa: E402
from fortify.security.models import FileToolPolicy  # noqa: E402


def _resolve_agent_name() -> str:
    """CLI arg wins over env; env wins over the default fallback."""
    if len(sys.argv) > 1:
        return sys.argv[1]
    return resolve_agent_name()


def _format_policy(policy: AgentPolicy) -> str:
    lines = [f"  default: {policy.default_policy.mode}"]
    for tool_name, tool_policy in policy.tools.items():
        line = f"  {tool_name:14s}  {tool_policy.mode}"
        if isinstance(tool_policy, FileToolPolicy) and tool_policy.file_scope:
            paths = tool_policy.file_scope.allowed_paths
            if paths:
                line += f"   allowed_paths={paths}"
        lines.append(line)
    return "\n".join(lines)


def main() -> int:
    import os

    if not os.environ.get("FORTIFY_KEY"):
        print("error: FORTIFY_KEY not set", file=sys.stderr)
        print(
            "mint a token at http://localhost:5173/tokens and put it in asianf/.env:",
            file=sys.stderr,
        )
        print("    FORTIFY_KEY=fty_test_support-bot_...", file=sys.stderr)
        return 2

    agent_name = _resolve_agent_name()
    try:
        config = FortifyConfig.from_env()
        client = FortifyClient(config)
        # get_agent returns (payload, etag) — the etag is for the SDK's
        # conditional-GET hot-reload path, which this demo doesn't need.
        payload, _etag = client.get_agent(agent_name)
    except FortifyError as exc:
        print(f"fortify error: {exc}", file=sys.stderr)
        return 1

    assert payload is not None, "first get_agent has no If-None-Match — 304 impossible"
    policy = AgentPolicy.model_validate(yaml.safe_load(payload["policy_yaml"]) or {})

    print(
        f"fetched '{agent_name}' from {config.base_url}/v1/projects/{config.project_id}"
    )
    print(f"updated_at: {payload['updated_at']}")
    print()
    print("policy:")
    print(_format_policy(policy))
    print()
    print("this same policy flows through create_agent -> enforce_policy")
    print("when you call load_agent(...) in a real runtime — GuardedTool")
    print("wraps every tool and calls authorize_tool_call on each invocation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
