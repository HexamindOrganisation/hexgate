---
name: write-policy
description: Write or modify a Hexgate YAML policy from a plain-language request — tool modes, argument constraints, caller attributes (ctx.*), run budgets (run.*), network egress (net.http_request / net.tcp_connect), agent admission and reach, roles and inheritance, or policy modules — then prove it with `hexgate policy validate` and `hexgate policy test` dry-runs. Use when asked to write, draft, generate, extend, tighten, or explain a Hexgate policy, or to turn "the agent may only…" into policy YAML.
---

# Write a Hexgate policy

The user describes intent in prose; you produce a policy YAML that **validates**,
plus a set of dry-run cases that **prove** it does what they asked. A policy you
have not run through the CLI is not finished.

Sources of truth — read the relevant one before using a feature you are unsure of,
and prefer them over this file if they disagree:

| Topic | File |
|---|---|
| Document shape, modes | `docs/policy/yaml-shape.mdx` |
| Constraint grammar (full reference) | `docs/policy/constraints.mdx` |
| Worked condition examples | `docs/policy/writing-conditions.mdx` |
| Roles, `policies/` dir, inheritance | `docs/concepts/user-scope.mdx` |
| Egress | `docs/concepts/egress.mdx` |
| Admission / agent reach | `docs/concepts/agent-level-enforcement.mdx` |
| Modules (boundaries / capabilities) | `docs/internals/policy-modules.md` |
| CLI | `docs/cli/policy.mdx` |
| Schema (authoritative) | `hexgate/security/models.py`, `hexgate/security/constraints.py` |
| Examples | `examples/demo_policy.yaml`, `deploy/demo_policies/` |

## Workflow

1. **Pin down the inputs.** You need:
   - the tool names and their argument names/types. Take these from the agent code, its `AgentManifest`, or a tools list in the project (e.g. a `TOOLS.md`). Otherwise, use what the requester named in their prompt, or ask them;
   - the roles (who calls);
   - the caller attributes available as `ctx.*`;
   - the shape to write in: a single file, a `policies/` dir, or modules.

   Never invent a tool or argument name. A typo'd tool is silently default-denied, and a typo'd arg fails closed. If a name is unknown, ask, or state the assumption explicitly.
2. **Restate the intent as a decision table** before writing YAML. List role × tool × condition → ALLOW / DENY / APPROVAL_REQUIRED, plus what happens to anything unlisted. Surface every ambiguity here, e.g. "refunds up to 500 — inclusive? which currency?".
3. **Write the YAML.** Start deny-by-default and grant the minimum. Put each constraint at the narrowest scope that works (see *Where constraints live*).
4. **Validate** until clean:
   ```bash
   uv run hexgate policy validate <file> --max-severity warning
   ```
   If `uv` fails building `biscuit-python` on Python 3.14, add `--python 3.13`.
   For a module tree, use `uv run hexgate policy check --dir <root> [--manifest m.json]` and `uv run hexgate policy resolve --dir <root>`.
5. **Prove it.** Turn every row of the decision table into a `policy test` run, including the negative cases and the boundary values (`== limit`, `limit + 1`, missing arg, wrong type):
   ```bash
   uv run hexgate policy test <file> --role billing --tool refund_order \
       --args '{"amount": 500, "currency": "USD"}' [--attributes '{"department": "finance"}']
   ```
   Also use `--roles a,b` to check a multi-role caller, where the most permissive role wins. Add `--engine wasm` when `opa` is installed, since that engine matches production. Every result must match the table. When one doesn't, fix the policy, not the table, unless the table was wrong.
6. **Report.** Give the YAML, the decision table with each case's actual CLI result, and any assumptions or residual risks, e.g. "egress on HTTPS is host-level only". State every assumption you made. If part of the request can't be expressed in a policy, or mustn't be done, say so plainly and don't approximate it silently.

## Never loosen a boundary on a team's request

In a module layout, `policies/boundaries/` is owned by security. Never widen one (raise a cap, remove a deny, relax a constraint) unless the request explicitly comes from security or asks for a boundary change. If a request needs more than a boundary allows, leave the boundary as it is, make the change within it, and tell the user which boundary blocks the rest and who has to change it. Adding a deny or tightening a boundary when asked is fine.

## Document shape

```yaml
version: 1
consts: { max_refund: 500, eu: ["EU", "UK"] }   # referenced as consts.<name>
constraints:                                    # policy-level: EVERY reachable key, incl. agent.*
  - run.tool_calls < 50
default_policy: { mode: deny }                  # catch-all for tools NOT listed below
tools:
  <tool_name>:
    mode: allow | deny | approval_required      # default: deny
    constraints: [ ... ]                        # AND-ed
admission: { mode: allow }                      # may this role run the agent (agent.run)
agents:                                         # which agents this one may reach
  billing-bot: { via: [tool, handoff], mode: approval_required }
```

**Multiple roles:** there are two layouts.
- A top-level `roles: {name: <policy as above>}`, where each role can have `inherits: [other]` and an `is_mixin: true` base.
- A `policies/<role>.yaml` file per role.

In a role-keyed document, only `version` and `constraints` may sit at file level. Everything else is a load error there, `consts` included, and must go inside a role or a mixin the roles inherit.

Always define a `default` role and keep it least-privilege, because unknown or absent roles fall back to it. Granting something only `default` grants trips the `permissive-default` lint; put such grants in a mixin instead.

**Inheritance:** a child's `tools` entry replaces the parent's entry for that tool. Policy-level `constraints:` accumulate instead.

**Modules:** use them when the user wants security-owned ceilings separate from team grants. The layout is:
- `policies/boundaries/*.yaml` holds ceilings and hard denies;
- `policies/capabilities/*.yaml` holds grants only (a deny there is an error);
- `roles.yaml` maps each role to its list of capabilities.

The composition rule is: fences intersect, grants union, and denies win. Three things are rejected in modules: policy-level `constraints:`, `default_policy.constraints`, and `file_scope`.

## Constraint cheat sheet

Each list item is one boolean expression, and every item must hold.

| Kind | Syntax |
|---|---|
| Compare | `==` `!=` `<` `<=` `>` `>=` · `in [..]` · `not in [..]` (RHS a JSON list or `consts.x`) |
| Operands | `args.a.b`, `args.max` (cross-field), `count(args.list)`, `consts.x`, JSON literals |
| Facts | `role`, `tool`, `ctx.<attr>` (trusted caller attributes), `run.<counter>` |
| Strings | `startswith(f, "s")` `endswith` `contains` `matches(f, "^re$")` (RE2, **unanchored**) |
| Lists | `every(args.xs, <cond on . or .field>)` (empty → true), `any(...)` (empty → false) |
| Bool | `and` `or` `not` `( )`; boolean composition (`and`/`or`/`not`) is rejected inside a quantifier body |

`run.*` counters: `tool_calls`, `calls_of_this_tool`, `tools_used` (list), `llm_calls`, `input_tokens`, `output_tokens`, `total_tokens`, `denials`, `approvals`, `errors`, `elapsed_seconds`, `id`, `agent`.

These are circuit breakers, not precise quotas. Read the warning in `constraints.mdx` before promising exact limits.

## Egress

Egress is not a separate section of the policy. Outbound traffic is gated as synthetic tools:
- `net.http_request` exposes `args.host`, `args.scheme` and `args.port`. `args.path` and `args.query` are visible on plain HTTP only.
- `net.tcp_connect` exposes `args.host`, `args.port` and `args.protocol` (`"tcp"`).

```yaml
tools:
  net.http_request:
    mode: allow
    constraints:
      - args.host in ["api.github.com"] or args.host == "example.com" or endswith(args.host, ".example.com")
      - args.scheme in ["https"]
  net.tcp_connect:
    mode: allow
    constraints:
      - args.host == "db.internal"
      - args.port in [5432]
```

Always include a host clause. For subdomains, use `endswith(args.host, ".example.com")` with the leading dot, which stops `evilexample.com` matching.

## Where constraints live

| Location | Applies to |
|---|---|
| `tools.<name>.constraints` | that tool only |
| `default_policy.constraints` | only tools **not** listed under `tools:` |
| top-level `constraints:` | every key the role can reach, **including** `agent.run` / `agent.*` |

## Pitfalls to check every time

- **Unquoted strings.** Write `args.env == "prod"`. `args.env == prod` fails `policy validate` with `bare identifier 'prod' ... did you forget quotes?`.
- **Unanchored regex.** `matches(args.id, "inv_")` also accepts `xxinv_yy`. Anchor it: `^inv_[0-9]+$`.
- **`args.*` at policy level.** Tools and `agent.run` without that argument fail closed, which can lock the agent out at admission. Keep `args.*` on the tool. Or exempt agent keys: `startswith(tool, "agent.") or args.amount <= 500`.
- **Run caps in `default_policy.constraints`.** They miss every tool listed under `tools:`. Put run caps in top-level `constraints:`.
- **`mode: deny` plus constraints.** Deny wins regardless. Constraints only narrow a grant; they never grant.
- **Admission is closed-world.** Once any role declares `admission`, every role without it is refused. `agents:` is closed-world too: an unlisted target is denied.
- **`file_scope`.** It is rejected by the WASM/Rego build, which is what production uses. Prefer constraints: `startswith(args.file_path, "src/")`, or `every(args.paths, startswith(., "src/"))`.
- **Strict types.** `true` is not `1`. A number compared against a string fails closed. A missing field fails closed.
- **Rate limits.** There are no per-time-window limits in YAML; say so. Use `run.*` caps or a Python guard (`docs/concepts/guards.mdx`).
- **Trust.** `ctx.*` and `role` are only as trustworthy as the server code that sets them. Mention this whenever a grant hinges on them.
