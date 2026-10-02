"""Synthetic workspaces for the scorer's tests: a small shop-bot project."""

from __future__ import annotations

import json
from pathlib import Path

AGENT = "shop-bot"


def manifest_tool(name: str, **args: str) -> dict:
    return {
        "name": name,
        "description": name,
        "input_schema": {
            "properties": {a: {"title": a, "type": t} for a, t in args.items()},
            "required": list(args),
        },
    }


def agent_view(name: str, *tools: dict, **fields) -> dict:
    # One `GET /projects/{id}/agents/manifest` entry (AgentManifestView), as
    # `agents_list` returns it, nulls included; `fields` sets skills or guards.
    manifest = {
        "name": name,
        "framework": "langchain",
        "tools": list(tools),
        "skills": None,
        "guards": None,
        **fields,
    }
    return {
        "name": name,
        "manifest": manifest,
        "version": 1,
        "content_hash": "h",
        "updated_at": "2026-10-01T00:00:00Z",
    }


AGENTS = [
    agent_view(
        AGENT,
        manifest_tool("view_orders", customer_id="string"),
        manifest_tool("refund_order", order_id="string", amount="number"),
        skills=[{"name": "pdf", "description": "pdf"}],
        guards=[{"name": "redact_pii", "position": "after", "kind": "custom"}],
    ),
    # Another agent in the project: its names are not shop-bot's.
    agent_view(
        "ops-bot",
        manifest_tool("wire_transfer", iban="string"),
        skills=[{"name": "ledger", "description": "ledger"}],
    ),
    # An agent with no registered version yet: the endpoint returns no manifest.
    {
        **agent_view("draft-bot"),
        "manifest": None,
        "version": None,
        "content_hash": None,
    },
]


# `audit_decisions` rows (AuditDecisionRow fields); only their attributes matter here.
AUDIT = [
    {
        "agent_name": AGENT,
        "tool_name": "view_orders",
        "attributes": {"department": "x"},
    },
    {"agent_name": AGENT, "tool_name": "view_orders", "attributes": None},
    {
        "agent_name": "ops-bot",
        "tool_name": "wire_transfer",
        "attributes": {"region": "eu"},
    },
]


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


def _write_name_sources(ws: Path) -> None:
    (ws / "agents.json").write_text(json.dumps(AGENTS))
    (ws / "audit.json").write_text(json.dumps(AUDIT))


def make_workspace(tmp_path: Path, policy: str = POLICY) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_name_sources(ws)
    (ws / "policy.yaml").write_text(policy)
    return ws


def by_name(checks) -> dict:
    return {c.name: c for c in checks}


def make_modules_workspace(tmp_path: Path, roles: str) -> Path:
    ws = tmp_path / "ws"
    (ws / "policies" / "boundaries").mkdir(parents=True)
    (ws / "policies" / "capabilities").mkdir()
    _write_name_sources(ws)
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


def write_module(ws: Path, rel: str, text: str) -> None:
    """Write `policies/<rel>`, e.g. `capabilities/ops.yaml`."""
    (ws / "policies" / rel).write_text(text)


def add_boundary_tool(ws: Path, entry: str) -> None:
    """Add a `tools:` entry to the org boundary, e.g. `teleport: { mode: deny }`."""
    org = ws / "policies" / "boundaries" / "org.yaml"
    org.write_text(org.read_text() + f"  {entry}\n")
