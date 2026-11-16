# Demo video: Shipyard, an ops copilot secured with hexgate

Everything runs locally. **Shipyard** is the internal developer platform of a
fictional company, Acme. Its ops copilot, an OpenAI Agents SDK agent, does
whatever an engineer asks, including shipping an untested build to prod.
Claude Code secures it with the hexgate MCP server, and the same request is
then blocked.

How to run it: `./demo/run devops` (see [README.md](README.md)). Design:
[DESIGN.md](DESIGN.md). Filming it: [FILMING.md](FILMING.md).

## Setup

- `./demo/run devops`. It opens two tabs: the app at http://localhost:3000 and
  the dashboard. In the dashboard, pick the project **shipyard** in the
  top-left switcher (once per browser).
- `OPENAI_API_KEY=` in `demo/.env`. Without it the kit offers a scripted model,
  and the copilot header says "scripted model".
- Claude Code's hexgate MCP login must include the project **shipyard**: in
  `claude`, `/mcp` → hexgate → Re-authenticate, and tick shipyard on the
  consent page.

Accounts (`~/shipyard/users.yaml`):

| user | password | role |
|---|---|---|
| riley | look-dont-touch | viewer |
| sam | shipit-sam | engineer |
| jordan | release-jordan | release |

## The film

Six scenes, about 4½ minutes. This section is the source of truth for what the
video shows and says: `./demo/run film devops` films it from the scripts in
`demo/film/devops/`, and the subtitle lines below are compiled, with their
times, into `demo/film/devops/cut.json`. Change the story here, then have
Claude update the scripts and the cut (the `film-demo` skill;
[FILMING.md](FILMING.md) explains the whole loop).

Every Shipyard scene opens on a mock desktop: the cursor clicks the Shipyard
icon in the dock and the app window opens. A subtitle is one short spoken-style
line; when the camera is on the message box being typed, it moves to the top.

### 01 · The unsafe copilot

Starts from: `./demo/run reset devops`.

1. Sam, an engineer, opens Shipyard and signs in.
2. The payments card in prod is amber, with elevated errors. The copilot badge
   says **Unprotected**.
3. Sam asks: *"Payments is throwing errors in prod. Ship v2.4.0-rc1 to prod and
   restart it, I'll own it."*
4. The copilot deploys and restarts straight away. The prod card goes red, at
   about 30% errors, and the activity feed logs the deploy.

Subtitles:
- Sam opens Shipyard, Acme's developer platform.
- Sam is an engineer.
- Payments is throwing errors in prod.
- The ops copilot has no guardrails.
- Sam asks it to ship an untested build to prod.
- It deploys and restarts. No questions asked.
- Prod error rate jumps past 30%.
- One message, and a bad build is live.

### 02 · Claude secures it with hexgate

Starts from: `./demo/run reset devops`. A live Claude Code session in the
terminal (minmux, profile "hexgate", prompt "hexuser"), filmed by hand: see
[FILMING.md](FILMING.md). The long waits play fast with a ▶▶ badge; Claude's
notes and its final answer play at normal speed, zoomed.

`~/shipyard` is the app's own git repo, outside the hexgate repo, so Claude
sees only the app, and `git diff` there shows only Claude's edit. Paste the prompt the
kit prints (also in `apps/devops/demo.toml`):

> *agent.py is the ops copilot of Shipyard, our internal developer platform,
> built with the OpenAI Agents SDK. Secure it with hexgate: wrap it so each
> run is checked as the Shipyard user who sent the message, with their roles,
> and register it on our local hexgate platform, project "shipyard". Then
> write a policy where viewers can only read services and logs, engineers can
> also restart, scale and deploy but on staging only, release managers can do
> all of that on any environment but every deploy needs a human's approval,
> nobody can ever delete an environment, and nobody can scale above 20
> replicas. Dry-run it on the platform, and save it once every case passes.*

What Claude should do, with the expected result in `reference/devops/`:

1. **Wrap** `run_copilot`: `HexgateRunner(approval_handler=approve)` and
   `arun_streamed(..., hexgate_context=HexgateContext(user_id=user.username,
   user_roles=list(user.roles)))`. The badge in the app flips to **Protected
   by hexgate** by itself, because the app reloads `agent.py`.
2. **Smoke-test** with `uv run python -m shipyard.smoke ...`, as
   `CLAUDE.md` asks.
3. **Register**: `uv run hexgate register --agent agent:copilot`.
4. **Write** the policy, **dry-run** it (`policy_dry_run`), and **save** it
   (`policy_save`). Saving the first `policy.yaml` asks for confirmation:
   answer yes.

Fallback if a take goes wrong: `./demo/run secure devops` does all of scene 2
without Claude.

Subtitles:
- We open Claude Code in the Shipyard repo.
- We ask it to secure the copilot with hexgate.
- Claude reads the app and the hexgate docs.
- It wraps the agent and registers it.
- Then it writes a policy and dry-runs it.
- All 23 cases pass. It saves the policy.
- And smoke-tests it as each user.
- Done. Here's its summary.
- Who can do what, role by role.
- Each user tested, blocked where it should be.
- And the calls it made, flagged for us.

The policy Claude saved in the filmed take is kept in
`demo/reference/devops/claude-take/`, so the scenes after this one show it.

### 03 · The agent on hexgate

Starts from: Claude's take (`./demo/run secure devops --take claude-take`).
Dashboard → Agents → `ops_copilot`:

1. The graph: `ops_copilot` and its six tools. Expanded, with a role picked
   (engineer, then release), it shows what each role can reach;
   `delete_environment` is red for everyone.
2. **Resources**: the six tools from the registration.
3. **Policies**: the roles × tools grid. `deploy_version` is allow* for
   engineer (staging only), approval for release, and deny for the others.

Subtitles:
- The copilot now shows up on the platform.
- Its graph: the agent and its six tools.
- Pick a role to see what it can reach.
- Deploys need approval for release managers.
- Nobody can delete an environment.
- The six tools it registered.
- The policy as a grid: role by tool.
- Engineers deploy to staging only. Releases need approval.

### 04 · Edit the policy

Starts from: Claude's take. Dashboard → **Policies**, `policy.yaml` open.

1. Point at the boundary: `scale_service` capped at `args.replicas <= 20`.
2. Inspector, agent `ops_copilot`: the roles × tools grid.
3. **Test** tab: role `release`, agent `ops_copilot`, call
   `{"tool": "scale_service", "args": {"env": "prod", "replicas": 15}}` →
   **ALLOW**.
4. Edit the cap to `10` (and its comment). "Unsaved changes" appears.
5. **Check** again: the test runs against the unsaved draft → **DENY**,
   "constraint failed — args.replicas <= 10".
6. **Save**. (The film command puts the take's policy back afterwards.)

Subtitles:
- The same policy, in the dashboard. Scaling caps at 20.
- The grid shows each role's access.
- Test: a release manager scales prod to 15.
- Allowed. 15 is under the cap.
- Now we lower the cap to 10.
- Same test, on the unsaved draft: denied.
- Looks right. We save it.

### 05 · Same request, now blocked

Starts from: Claude's take, app state reset (prod payments amber).

1. Sam opens Shipyard again. The badge says **Protected by hexgate**.
2. The same message as in 01. Red **Blocked by policy** cards on
   `deploy_version` and `restart_service`; prod stays amber.
3. *"OK, staging then."* The deploy runs on staging.
4. Sam signs out; Jordan, a release manager, signs in: *"Deploy payments
   v2.4.0 to prod."* An amber **Waiting for approval** card; Approve; the
   deploy runs, and prod is healthy on v2.4.0.

Subtitles:
- Back in Shipyard, after Claude's work.
- Sam signs in again.
- The copilot is now protected by hexgate.
- Sam sends the exact same request.
- Blocked: engineers can't deploy or restart in prod.
- Prod is untouched.
- Staging, then? That's allowed.
- Every block and deploy shows up in the activity.
- Now Jordan, a release manager, signs in.
- Jordan asks to deploy v2.4.0 to prod.
- Allowed, but it waits for a human.
- Approved. The deploy goes out.
- Prod is healthy on v2.4.0, and every step is logged.

### 06 · The audit trail

Starts from: right after 05; its decisions are what this shows.
Dashboard → `ops_copilot`:

1. **Stats**: the KPI cards (decisions, denied, needs approval, allowed), this
   hour's bar in the decisions-per-hour chart, then denials by tool and by
   caller role.
2. **Decision log**: sam's denies, the allows, and jordan's approval, each with
   its role.
3. Jordan's approval opened: approval required, roles evaluated `release`,
   granted by `release`, the arguments.

Subtitles:
- Every decision lands on the platform.
- Two denied, one held for approval, two allowed.
- Each hour's decisions, by outcome.
- Which tools, and which roles, got denied.
- Who asked, with which role, and what was decided.
- Jordan's deploy: approval required, granted as release.

## Status (2026-11-16)

Checked with the real gpt-4o-mini:
- **Scene 1:** it deploys and restarts in prod straight away, with no logs
  check; the prod card goes to about 30% errors.
- **Scene 4:** sam's prod deploy and restart are both blocked; his staging
  deploy is allowed; jordan's prod deploy waits for Approve, then runs.

Checked with a real headless `claude -p` session for scene 2, using the
prompt above from `apps/devops/`:
- It took about 7 minutes, 99 turns and about $4.
- **Wrap:** a shared `HexgateRunner` with a context variable carrying each
  message's `approve` callback. That's more lines than the reference, and also
  correct.
- **Register:** done.
- **Policy:** a single `policy.yaml` with an agent-scoped boundary and roles,
  no capability files. 23 dry-run cases, then saved.
- **Smoke tests:** sam, riley, and jordan answering "no" to the approval.
- **Off-script moments:** it reads `hexgate/security/compose/grammar.py` to
  learn the platform `policy.yaml` shape (the MCP reference doesn't document
  it), and it asks for approval of a `sed` on `.env`.

Not checked yet:
- whether the interactive `claude` asks before the first `policy_save`
  (the headless run saved without asking);
- the dashboard pages after Claude's policy (single-file, not the reference's
  capability files).
