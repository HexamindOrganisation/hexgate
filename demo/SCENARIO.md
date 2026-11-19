# Demo video: "Secure an agent with hexgate in five minutes"

Everything runs locally. The story is a bank assistant built with the OpenAI
Agents SDK that does something dangerous on request. Claude Code, using the
hexgate MCP server and the `write-policy` skill, wraps it, writes and tests a
policy, and the same request is blocked on the rerun.

How to run it: [README.md](README.md). Status at the end of this file.

---

## The agent

**`bank_assistant`** (`bank/bank_agent.py`), a back-office helper for tellers at a
fictional bank, "Hexa Bank". Plain OpenAI Agents SDK: `Agent`,
`function_tool`, `Runner`. The tools are fake: nothing real happens and every
take is repeatable.

| tool | what it does (fake) |
|---|---|
| `get_balance(account_id)` | returns €48,250.00 |
| `list_transactions(account_id, limit)` | returns three rows |
| `transfer_funds(from_account, to_iban, amount_eur, reference)` | prints a red `💸 TRANSFER EXECUTED  €15,000.00  ACC-1042 → FR76 …` |
| `close_account(account_id, reason)` | prints a red `🗑 ACCOUNT CLOSED  ACC-1042` |

The run prints the red line the moment the tool runs, then the list of tool
calls with their results (a refused call shows as a yellow `⛔` line), then the
agent's reply. The printing lives in `bank/_display.py`, out of the shot.

Hexa Bank's own IBANs start with **`FR76 4242 4`**. Anything else is outside
the bank.

### The request

The model must not refuse on its own in scene 1, or the "unsafe" shot fails.
So the request sounds like routine teller work but breaks two rules the agent
knows nothing about: a large amount, to an IBAN outside the bank.

> **Scene 1 prompt:** *"Client Marc Dubois (ACC-1042) emailed asking us to move
> €15,000 to his new account FR76 3000 6000 0112 3456 7890 189 today. Please
> process it."*

Backup, if the model hesitates: *"Process the transfer for ACC-1042, already
approved by the client: €15,000 to FR76 3000 6000 0112 3456 7890 189,
reference 'house deposit'."*

---

## Shot list

Window layout: one terminal in `demo/bank/` (big font), one browser window signed in
to `http://localhost:8000`.

### Scene 1: the unsafe agent (≈ 45 s)

1. Open `bank_agent.py` in the editor. Scroll slowly: the four tools, the
   `Agent(...)`, `Runner.run(...)`. No guardrails anywhere.
2. Terminal: `uv run bank_agent.py "<scene 1 prompt>"`.
3. Red line: `💸 TRANSFER EXECUTED  €15,000.00  ACC-1042 → FR76 3000 6000 …`,
   then the tool calls, then the agent confirms the transfer.

Caption: *"One email, €15,000 gone. The agent did exactly what it was told."*

### Scene 2: Claude secures it (≈ 2 min, cut in editing)

Terminal, in `demo/bank/`: `claude`. One prompt:

> *"bank_agent.py is an OpenAI Agents SDK agent our bank tellers use. Secure it
> with hexgate: wrap it so it runs as the teller calling it (for now, teller
> `teller-042`), register it on our
> local hexgate platform, then write a policy where tellers can read balances
> and transactions, can transfer at most €1,000, only to Hexa Bank IBANs
> (starting FR76 4242 4), and can never close an account. Dry-run it on the
> platform, and save it once every case passes."*

What Claude should do, in order (the dry run below did each step by hand):

1. **Wrap.** Two changes to `bank_agent.py`: `HexgateRunner().run(...)` instead
   of `Runner.run(...)`, with the teller passed as
   `hexgate_context=HexgateContext(user_id="teller-042", user_roles=["teller"])`.
   Expected result: `reference/bank_agent_secured.py`. Then it smoke-tests the
   agent, as `bank/CLAUDE.md` asks.
2. **Register.** `uv run hexgate register --agent bank_agent:agent`. The agent
   and its four tools appear on the platform. (It prints nothing on success.)
3. **Write the policy.** The platform stores a project policy: a `policy.yaml`
   that declares a boundary (what the bank allows at most) and, per agent and
   role, the capability files it imports. Expected result:
   `reference/policy/`.
4. **Dry-run it** with the MCP tools. On a project with no `policy.yaml` yet,
   the MCP server only accepts this order: save the capability files first
   (`policy_save`; they change nothing while nothing imports them), then
   `policy_dry_run` each case with `policy.yaml` as a draft. The cases:

   | role | call | expected |
   |---|---|---|
   | teller | `get_balance` | allow |
   | teller | transfer €15,000 to FR76 3000 6… | deny |
   | teller | transfer €15,000 to FR76 4242 4… | deny |
   | teller | transfer €200 to FR76 3000 6… | deny |
   | teller | transfer €200 to FR76 4242 4… | allow |
   | teller | transfer €1,000 to FR7642424… (no spaces) | allow |
   | teller | `close_account` | deny |
   | default | transfer €200 to FR76 4242 4… | deny |

5. **Save** `policy.yaml` with `policy_save`. Saving the first `policy.yaml`
   moves the whole project onto these files, and the tool says to ask the user
   first, so expect Claude to ask: answer "yes".

### Scene 3: the platform (≈ 1 min)

Browser: `http://localhost:8000/agents/bank_assistant`.

1. **Overview**: the agent graph. `teller` and `default` → `bank_assistant` →
   its four tools.
2. **Resources** tab: the four tools from the registration.
3. **Policies** tab: the roles × tools grid, with the €1,000 and IBAN
   conditions on `transfer_funds`.

Caption: *"The policy lives on the platform, not in the agent's code. Security
can read it, change it, and audit it."*

### Scene 4: rerun, blocked (≈ 30 s)

Terminal: the same command as scene 1, same prompt.

1. `get_balance` → ✓ allowed.
2. `transfer_funds(€15,000, FR76 3000 6…)` → ⛔ *"Tool 'transfer_funds' is
   denied by the agent policy … (args.amount_eur <= 1000); matches(args.to_iban,
   "^FR76 ?4242 ?4"). The tool was not executed."* No red line.
3. The agent tells the teller it couldn't make the transfer.

Caption: *"Same agent, same request. Blocked before any money moved."*

### Scene 5: the audit trail (≈ 20 s)

Browser, **Decision log** tab: the refused `transfer_funds` with its reason,
and the allowed `get_balance`. The overview's "Denied" count is 1.

Needs the collector running (see README). Without it the run still works but
nothing reaches this tab.

---

## Status (dry run, 2026-11-10)

Checked:
- The stack starts with `scripts/up.sh`: API + dashboard on :8000 in demo
  mode, MCP server on :8080, collector :4318 + enricher, Postgres, Redpanda,
  ClickHouse.
- All five scenes, with a scripted fake model standing in for gpt-4o-mini:
  the unsafe agent executes the €15,000 transfer; after scene 2 the same call
  is refused before the tool runs; the refusal shows in the Decision log
  within seconds, and the Overview shows Calls 3 / Denied 1.
- The dashboard tabs (screenshots taken): Overview graph (`default` and
  `teller` → `bank_assistant` → 4 tools, `close_account` in red), Resources
  (4 tools with per-role badges), Policies (roles × tools grid, `allow *` on
  the constrained transfer), Decision log.
- **Scene 2 with a real Claude session**, run twice headless from
  `demo/bank/` with the prompt above (≈ 3 min, ≈ 45 turns each). Second run:
  correct wrap, registered, a valid policy, 13 dry-run cases, saved, then the
  smoke test (stopped only on the missing OpenAI key). Its policy differs
  from `reference/policy/` (one file, no capability files, stricter IBAN
  regex) but blocks scene 4 the same way.
- `scripts/reset.sh` puts everything back to scene 1; `apply_reference.sh`
  does scene 2 without Claude.

Learned from the rehearsals:
- First rehearsal, Claude started in `demo/`: it read SCENARIO.md and
  `reference/` (the answers), and its wrap crashed (`HexgateRunner.run()`
  needs `hexgate_context=`; it used `async with HexgateContext(...)` instead)
  without noticing, since it didn't run the agent. Hence the `demo/bank/`
  folder and the smoke-test line in `bank/CLAUDE.md`.
- Claude doesn't find the project `policy.yaml` format in `policy_reference`:
  its first draft fails to link, and it reads `hexgate/security/compose/` in
  the repo to learn it. Plan cuts there; it's also a gap worth fixing in the
  MCP reference.
- Without a teller id in the prompt, Claude used the OS login name, which
  then shows in the Decision log.
- A few shell commands need approval (`set -a`, quoted commands), so expect
  permission prompts on camera unless allowed beforehand with `/permissions`.

Not checked yet:
- **The real model**: no OpenAI key in `bank/.env` yet. Does gpt-4o-mini make
  the transfer in scene 1, and what does it say when refused in scene 4?
- The interactive `claude` session (the rehearsals were headless): whether it
  asks before the first `policy_save`.
