"""Synthetic workspaces for the scorer's tests: a small shop-bot project."""

from __future__ import annotations

from pathlib import Path

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


def write_project_files(ws: Path) -> None:
    (ws / "README.md").write_text("# shop-bot\n")


def make_workspace(tmp_path: Path, policy: str = POLICY) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    write_project_files(ws)
    (ws / "policy.yaml").write_text(policy)
    return ws


def by_name(checks) -> dict:
    return {c.name: c for c in checks}


def make_modules_workspace(tmp_path: Path, roles: str) -> Path:
    ws = tmp_path / "ws"
    (ws / "policies" / "boundaries").mkdir(parents=True)
    (ws / "policies" / "capabilities").mkdir()
    write_project_files(ws)
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
