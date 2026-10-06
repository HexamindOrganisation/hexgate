# Policy-writing eval set

This set measures how well an AI agent turns a plain-English request into a
Hexgate policy. It is developer tooling: nothing here ships or runs in the
product.

- `cases/<category>/<name>/` holds one case each. The case's id is
  `<category>/<name>`, and the category is its scoring group.
- `starting_projects/<name>/` holds the projects several cases start from.
- `cases.py` loads the cases, and `calls.py` checks and completes their dry-run
  calls. `checks.py` scores what an agent left behind.

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

There is no `id` or `category` field: both come from the path. The loader
rejects anything that would otherwise drop a check without a word:

- **The case file:** `id` or `category` set in it; a field, `expect` key or
  decision key it doesn't know; a string where a list belongs; an `agent` with
  no manifest in `agents.json`.
- **Paths:** an `unchanged` path the starting project lacks; anything else in
  the case folder (a `wrong_answers/`); a `preserve.yml`.
- **Names in a dry-run call** (`calls.py`, `unknown_names`): a tool, argument,
  caller attribute, reached agent or skill the case's agent doesn't know (see
  *A starting project*).
- **Values in a dry-run call** (`calls.py`, `bad_values`):
  - for one of the agent's tools, a missing required argument, or a value of
    another type than its schema's: a quoted `"51"`, or a blank `amount:`,
    which YAML reads as null. A `string` in the manifest checks nothing, since
    adapters write it for `int | None` too;
  - a caller attribute of another JSON type than its audit rows carry;
  - a date or any other value that isn't JSON.
- **Synthetic calls** (`calls.py`, `complete`), which must match what the gate
  sends:
  - `agent.run` and `agent.<via>:<target>`: no args at all, since the gate
    sends its own (`agent`, `target`, `via`) and the scorer fills them in;
  - `skill:` / `skill.resource:` / `skill.script:`: `skill` and `via`, plus a
    `file_path` and a `content_hash` (null allowed), and for a script its
    `script_args`, `short_options` and `positional_args` (null when unused);
  - `net.http_request`: an upper-case `method` and an absolute `http://`
    `url`, or `host` and `port` for `CONNECT`, the only way HTTPS reaches the
    proxy; the rest must be what the proxy derives from them;
  - `net.tcp_connect`: `host` and an int `port`; `protocol` is `tcp`.

  The loader fills in what a skill or `net.*` gate derives, and the scorer
  fills in an agent gate's args, so a case can write just
  `{tool: "agent.tool:billing-bot"}` or
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
  the tools, arguments and skills in its own manifest, plus the synthetic
  `net.*`, `agent.run`, `agent.<via>:<target>` (a target must be an agent in
  this list) and `skill:` / `skill.resource:` / `skill.script:` calls.
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
