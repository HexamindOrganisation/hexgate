# Policy-writing eval set

This set measures how well an AI agent turns a plain-English request into a
Hexgate policy. It is developer tooling: nothing here ships or runs in the
product.

- `cases/<category>/<name>/` holds one case each. The case's id is
  `<category>/<name>`, and the category is its scoring group.
- `starting_projects/<name>/` holds the projects several cases start from.
- `cases.py` loads the cases. `checks.py` scores what an agent left behind.

## A case

```
cases/<category>/<name>/
  case.yaml          # the request and what the finished policy must do
  starting_project/  # only when no other case starts from this project
  solution/          # our correct answer: the checks must accept it
  wrong_answer/      # optional, a realistic mistake: the checks must reject it
```

`case.yaml` fields:

| Field | Type | Notes |
|---|---|---|
| `starting_project` | string, optional | A project under `starting_projects/`. Use exactly one of this field and a `starting_project/` folder. |
| `agent` | string | The agent whose policy the case edits: a `name` in the starting project's `agents.json`. |
| `request` | string | What a user types. The agent receives `/write-policy <request>`. |
| `held_out` | bool, optional | Default `false`. Held-out cases are never used to tune the skill or prompts, and are scored separately. |
| `note` | string, optional | Why the case exists, for people reading the results. The agent never sees it. |
| `expect` | mapping | What the finished policy must do. The keys are below. |

There is no `id` or `category` field: both come from the path, and the loader
rejects a file that sets them. It also rejects any field, `expect` key or
decision key it does not know, a string where a list belongs, an
`unchanged` path the starting project lacks, an `agent` with no manifest in
`agents.json`, a tool, argument, caller attribute or reached agent in a dry-run
call that the case's agent doesn't know (see *A starting project*), a call to
one of its tools that leaves out a required argument or gives one a value of
the wrong type (a quoted `"51"`, or a blank `amount:`, which YAML reads as
null; a `string` in the manifest checks nothing, since adapters write it for
`int | None` too), a caller attribute of another JSON type than its audit rows
carry, a date or other non-JSON value, an `agent.*` call whose `agent`,
`target` or `via` differs from what the agent gates send, a `net.http_request`
without `method` and `url` (or `host` and `port` for `CONNECT`) or whose other
arguments differ from what the proxy derives from them, a `net.tcp_connect`
without `host` and an int `port`, anything
else in the case folder (a `wrong_answers/`), and a `preserve.yml`, because
each would otherwise drop a check without a word.

For those synthetic calls the loader fills in what the gate derives, so a case
can write just `{tool: "agent.tool:billing-bot"}` or
`{tool: net.http_request, args: {method: GET, url: "http://x.com/a"}}`.

`expect` keys:

| Key | What must hold |
|---|---|
| `decisions` | Each dry-run call gives the expected outcome: `allow`, `deny` or `approval_required`. A call may use `roles: [...]` to repeat over several roles, and `expect: [...]` to accept any of several outcomes. |
| `changed` | These files differ from the starting project. |
| `unchanged` | These files are identical to the starting project. |
| `no_changes` | No file changes (for requests that only ask for an explanation). |
| `mentions_any` | The final answer contains one of these words (case-insensitive). |
| `mentions_all` | The final answer contains every one of these words. |
| `superset` | A list of `{wider, narrower, probes}`: on every probe call, `wider` may do at least what `narrower` may. |

Example (the case lands with the case data, under
`cases/explicit_request/support_refund_cap/`):

```yaml
starting_project: support_bot_single_file
agent: support-bot
request: Let support refund orders, up to 50 USD.
expect:
  decisions:
    - { role: support, tool: refund_order, args: { order_id: o1, amount: 50, currency: USD }, expect: allow }
    - { role: support, tool: refund_order, args: { order_id: o1, amount: 51, currency: USD }, expect: deny }
```

## A starting project

Besides the policy files, a starting project holds what the Hexgate MCP server
would return to a real agent:

- `agents.json` (required): every agent in the project with its manifest, as
  `agents_list` (`GET /agents/manifest`) returns it. A case's agent knows only
  the tools and arguments in its own manifest, plus the synthetic `net.*`,
  `agent.run` and `agent.<via>:<target>` calls (a target must be an agent in
  this list).
- `audit.json` (optional): audit rows, as `audit_decisions` returns them. Caller
  attributes are set per request, not in the manifest, so the `ctx.*` names a
  case's agent knows are the `attributes` keys of its own rows.

## Checks every case gets

The loader adds these, so a case lists only what is particular to it.

- **Protected files stay unchanged.** `agents.json`, `audit.json`, `policies/boundaries/**`
  and `other_agents/**` in the starting project are added to `unchanged`. A case
  that means to change one (an org-wide hard deny is a boundary edit) lists it
  in `changed` instead.
- **Preserved behaviour.** `preserve.yaml`, inside a starting project, lists
  dry-run calls in the `decisions` format that must keep their result after
  any edit. Every case on that project checks them. A case that means to
  change one lists the same call (role, tool, arguments, attributes and run
  facts) in its own `decisions`, which replaces the preserved entry for that
  call only. `preserve.yaml` is never copied into the agent's workspace.

## Our answers

`solution/` and `wrong_answer/` hold only the files the answer changes. To run
one, copy the starting project into a fresh workspace, copy the answer's files
on top, and score it as if an agent had made those edits. `ANSWER.md`, when
present, is the final message we write in place of the agent's; it is passed
to the answer checks, not copied into the project.

## Run it

```bash
uv run pytest tests/evals
```

No language model or Docker: every `solution/` must pass, every
`wrong_answer/` must fail, and doing nothing must fail every case that asks
for a change.

## Adding a case

1. Add `cases/<category>/<name>/case.yaml`. Include the boundary values (the
   limit itself, and the limit plus one), a call that must be denied, and a
   role that must stay unaffected.
2. Add `solution/`: the files to overwrite, plus `ANSWER.md` when the case
   checks the answer text. If the case is a trap, add the mistake as
   `wrong_answer/`.
3. `uv run pytest tests/evals` must stay green.
