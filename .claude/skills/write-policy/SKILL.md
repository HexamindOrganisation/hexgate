---
name: write-policy
description: Write or modify a Hexgate YAML policy from a plain-language request — tool modes, argument constraints, caller attributes (ctx.*), run budgets (run.*), cross-run usage quotas (agent_usage.*, user_usage.*), network egress (net.http_request / net.tcp_connect), agent admission and reach, roles and inheritance, or policy modules — then prove it with `hexgate policy validate` and `hexgate policy test` dry-runs. Use when asked to write, draft, generate, extend, tighten, or explain a Hexgate policy, or to turn "the agent may only…" into policy YAML.
---

# Write a Hexgate policy

The user describes intent in prose; you produce a policy YAML that **validates**,
plus a set of dry-run cases that **prove** it does what they asked. A policy you
have not run through the CLI is not finished.

## Read first

Before writing anything, read these two pages in full. They hold the method and
the traps, and they are the source of truth this skill follows:

- `docs/policy/writing-a-policy.mdx`: the method, step by step;
- `docs/policy/common-mistakes.mdx`: agent reach, egress, safe patterns, scope,
  usage caps and trust.

The paths are from the repository root, and from the workspace in the eval
sandbox. If they are not there, read the same pages through the Hexgate MCP
server's `policy_reference` tool (sections `writing-a-policy` and
`common-mistakes`), or at `https://docs.hexgate.ai/policy/writing-a-policy` and
`https://docs.hexgate.ai/policy/common-mistakes`.

Read the others when you need a detail, and prefer them over this file if they
disagree:

| Topic | File |
|---|---|
| Document shape, modes | `docs/policy/yaml-shape.mdx` |
| Constraint grammar, `run.*`, `agent_usage.*`, `user_usage.*` | `docs/policy/constraints.mdx` |
| Worked condition examples | `docs/policy/writing-conditions.mdx` |
| Roles, `ctx.*`, `policies/` dir, inheritance | `docs/policy/roles-and-attributes.mdx` |
| Egress | `docs/policy/egress.mdx` |
| Admission / agent reach | `docs/policy/agents.mdx` |
| What validation checks, dry-run rules | `docs/policy/testing-a-policy.mdx` |
| Cross-run quotas, fail mode | `docs/adr/R-USAGE-001-cross-run-usage-is-soft-and-fails-open.md` |
| CLI commands | `docs/cli/policy.mdx` |
| Schema (authoritative) | `hexgate/security/models.py`, `hexgate/security/constraints.py` |
| Examples | `examples/demo_policy.yaml`, `deploy/demo_policies/` |

## Workflow

Follow the six steps of `writing-a-policy.mdx`: pin down the inputs, record what
an existing policy allows today, restate the intent as a decision table, write,
validate and prove, report. With the CLI, they map to these commands.

**Inputs.** Take tool and argument names from the agent code or its manifest
JSON; keep the manifest file to pass to `validate`.

**Record what the policy allows today**, and **prove** it, with `policy test`:

```bash
uv run hexgate policy test <file> --role billing --tool refund_order \
    --args '{"amount": 500, "currency": "USD"}' [--attributes '{"department": "finance"}'] \
    [--agent-usage '{"invocations_1h": 100}'] [--user-usage '{"invocations_1h": 50}'] --engine wasm
```

- `--roles a,b` checks a caller holding several roles.
- `--agent-usage` and `--user-usage` feed `agent_usage.*` and `user_usage.*`; a
  path left out reads zero.
- Pass `--engine wasm` on every run, since that engine matches production. If it
  fails because OPA itself is unusable (`opa not found`, `HEXGATE_OPA_BIN points
  to…`, `below the tested floor`), drop the flag and say in the report that the
  production engine went unchecked, and why; any other wasm error is a problem
  in the policy to fix.
- `policy test` takes one file. For a module tree, resolve a role to a file
  first with `uv run hexgate policy resolve --dir <root> --role <role>`, then
  test that file. That file holds one role only, so `--roles a,b` cannot run on
  it, and whether admission or reach is declared is judged on that role alone,
  while the runtime judges it across every role: dry-runs of `agent.run` and
  `agent.*` keys can then differ from production.
- `policy test` reports deny for `agent.handoff:*` and `agent.tool:*` even when
  the policy declares no such gate, while the runtime then lets the call
  through: read the gate rules in `common-mistakes.mdx`, not only the dry-run.

**Validate** until clean:

```bash
uv run hexgate policy validate <file> --max-severity warning [--manifest m.json]
uv run hexgate policy check --dir <root> [--manifest m.json]   # a module tree
```

If `uv` fails building `biscuit-python` on Python 3.14, add `--python 3.13`.

**Report** the YAML, the decision table with each row's actual CLI result, the
assumptions, and any residual risk.
