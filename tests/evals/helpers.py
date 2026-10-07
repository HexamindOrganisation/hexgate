"""Synthetic workspaces for the scorer's tests: a small shop-bot project."""

from __future__ import annotations

from pathlib import Path

from evals.policy_writing.policy import Policy, effective_policy

AGENT = "shop-bot"


POLICY = """\
version: 1
roles:
  default:
    tools:
      view_orders: { mode: allow }
  support:
    tools:
      view_orders: { mode: allow }
      refund_order:
        mode: approval_required
  billing:
    tools:
      view_orders: { mode: allow }
      refund_order:
        mode: allow
        constraints:
          - args.amount <= 500
"""


# Parses and builds, but `default` grants a tool no named role grants: a warning
# at the CLI's default threshold, a failure at ours.
PERMISSIVE_DEFAULT = """\
version: 1
roles:
  default:
    tools:
      refund_order: { mode: allow }
  billing:
    tools:
      view_orders: { mode: allow }
"""


def make_workspace(tmp_path: Path, policy: str = POLICY) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("# shop-bot\n")
    (ws / "policy.yaml").write_text(policy)
    return ws


def by_name(checks) -> dict:
    return {c.name: c for c in checks}


def valid_policy(ws: Path, agent: str | None = None, modules: bool = False) -> Policy:
    policy, problems = effective_policy(ws, agent, modules)
    assert problems == []
    return policy


def install_skill(ws: Path) -> None:
    """As the harness does: under a dot path, so not a project file."""
    skill = ws / ".claude" / "skills" / "x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("installed by the harness")


# `billing` adds payments to the read-only `default`.
ROLES = "  default: [read_only]\n  billing: [read_only, payments]\n"


def make_modules_workspace(tmp_path: Path, roles: str = ROLES) -> Path:
    ws = tmp_path / "ws"
    (ws / "policies" / "boundaries").mkdir(parents=True)
    (ws / "policies" / "capabilities").mkdir()
    (ws / "README.md").write_text("# shop-bot\n")
    (ws / "policies" / "boundaries" / "org.yaml").write_text(
        "default_policy: { mode: allow }\n"
        "tools:\n"
        '  refund_order: { mode: allow, constraints: ["args.amount <= 1000"] }\n'
    )
    (ws / "policies" / "capabilities" / "read_only.yaml").write_text(
        "tools:\n  view_orders: { mode: allow }\n"
    )
    (ws / "policies" / "capabilities" / "payments.yaml").write_text(
        "tools:\n  refund_order: { mode: allow }\n"
    )
    (ws / "roles.yaml").write_text(f"version: 1\nroles:\n{roles}")
    return ws
