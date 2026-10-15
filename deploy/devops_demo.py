"""Hexgate DevOps demo (marimo) — a 3-level deploy chain, gated per role.

A live showcase of **nested sub-agents at depth** on a DevOps scenario: the only
agent you talk to is `developer`, and the privileged ops (`restart_service`,
`deploy_version`) live three hops away on `deploy_bot`. To use them, `developer`
must escalate to `release_lead`, which must escalate to `deploy_bot`:

    you → developer → release_lead → deploy_bot → restart_service / deploy_version

Each hop is an **agent-as-tool** reach, gated under the *calling* agent's policy
(`agent.tool:release_lead` on developer, `agent.tool:deploy_bot` on release_lead),
and the caller's role rides the ambient context down all three levels. The
stage/approval caps fire at the leaf. Drive it in the Playground:

  * as `viewer` — starts developer and reads service status, but is **not granted
    the reach** to release_lead, so a deploy request is denied at the first hop.
  * as `engineer` — escalates the whole chain; `deploy_bot` restarts/deploys
    **staging** only (a `prod` target is denied *inside* deploy_bot, after 3 hops).
  * as `release` — escalates the same chain; restarts **any** env, and a
    `deploy_version` to any env is **approval-gated** (change control) — the deploy
    prompt surfaces in the Playground before it runs.

All three agents are their own compose agents: serve registers the whole tree and
binds each one's dashboard-editable platform policy (the serve path binds every
sub-agent, at any depth, not just the root). The policy is the **compose**
`policy.yaml` + capability files seeded into the showcase project (see
`platform/api/.../policy_modules/devops_seed.py`).

Run with `make demo-devops` — boot.py brings up the platform + dashboard scoped to
the showcase project; paste your OpenAI key here and click Start.
"""

import marimo

__generated_with = "0.24.0"
app = marimo.App(width="medium")


@app.cell
def _():
    import sys
    import time
    from pathlib import Path

    import marimo as mo

    # serve_manager lives next to this file; make it importable when marimo runs
    # the notebook from an arbitrary CWD. The resolved-policy tables below are pure
    # resolution (no platform needed); the served tree talks to the live platform.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import serve_manager

    from hexgate.security.compose import resolve_file

    return Path, mo, resolve_file, serve_manager, time


@app.cell
def _(mo):
    mo.md("""
    # 🚀 Hexgate — a 3-level deploy chain, gated by a *compose* policy

    **developer** is the only agent you talk to. It can read service status, but it
    has **no deploy tool** — the privileged `restart_service` / `deploy_version` live
    three hops away on **deploy_bot**. To use them the chain must escalate:

    > you → **developer** → **release_lead** → **deploy_bot** → `restart_service` / `deploy_version`

    Each hop is an **agent-as-tool** reach, gated under the *calling* agent's policy
    (`agent.tool:release_lead` on developer, `agent.tool:deploy_bot` on release_lead).
    The caller's role rides the ambient context down all three levels, and the
    stage/approval caps fire at the leaf: `engineer` acts on **staging** only;
    `release` restarts **any env**, and a version deploy to any env needs
    **approval** (change control). `viewer` reads status but isn't granted the reach,
    so a deploy dies at the first hop.

    All three agents are their own compose agents — serve registers the whole tree
    and binds each one's **dashboard-editable** platform policy (the serve path binds
    every sub-agent, at any depth). Paste your OpenAI key below (used only in this
    kernel), serve `developer`, and drive it in the **Playground**.
    """)
    return


@app.cell
def _(Path, mo):
    # Under `make demo-devops`, boot.py writes the running dashboard URL here and
    # /v1/demo-login signs you in. Standalone (`marimo edit`) there's no dashboard —
    # guard the read and show how to launch one.
    try:
        _dash = Path("/tmp/hexgate_dash_url").read_text().strip().rstrip("/")
    except OSError:
        _dash = None
    if _dash:
        _banner = mo.md(
            f"""
            ### [▶ Open the live dashboard →]({_dash}/v1/demo-login)

            Signs you in on the dashboard, scoped to the seeded showcase project.
            Start the agent below, then chat with it in the **Playground** — the
            Policies editor shows the very policy gating each tier of the chain.
            """
        )
    else:
        _banner = mo.callout(
            mo.md(
                "Launch the **platform + dashboard + this agent together** with "
                "`make demo-devops` (→ http://localhost:2718) to chat in the live "
                "**Playground**. The policy tables below also run standalone."
            ),
            kind="info",
        )
    _banner
    return


@app.cell
def _():
    # -- Tools + agents --------------------------------------------------------
    # A 3-level chain built bottom-up: deploy_bot (holds restart_service +
    # deploy_version) → release_lead (reaches deploy_bot) → developer (reaches
    # release_lead, the root we serve). Each child is a plain, named HexgateAgent
    # mounted on its parent with `child.as_tool()`; serve registers the whole tree
    # (auto_register_subagents) and binds each node's dashboard-editable platform
    # policy — so we build them PLAIN here, no in-kernel enforcement and no build-time
    # credential. Each tier's role-aware policy lives in its compose block
    # (devops_seed.py / _FILES below), editable in the dashboard's Policies tab.
    from langchain_core.tools import tool

    from hexgate import create_agent

    def build_deploy_bot():
        """L3 — the deploy bot that holds the privileged restart + deploy tools."""

        @tool
        def restart_service(service: str, env: str) -> str:
            """Restart a service in an environment (e.g. "staging" or "prod")."""
            return f"(demo) restarted {service} in {env}."

        @tool
        def deploy_version(
            service: str, version: str, env: str, config: str = "default"
        ) -> str:
            """Roll out a new version of a service to an environment with a config."""
            return (
                f"(demo) deployed {service} {version} to {env} (config: {config})."
            )

        deploy_bot, _handler = create_agent(
            model="gpt-4o-mini",
            tools=[restart_service, deploy_version],
            system_prompt=(
                "You are the deploy bot. Restart a service with restart_service"
                "(service, env), or roll out a new version with deploy_version"
                "(service, version, env, config). Confirm the service, version, "
                "environment, and config profile."
            ),
            name="deploy_bot",
        )
        return deploy_bot

    def build_release_lead():
        """L2 — the release lead. Has no deploy tool; escalates to deploy_bot."""
        deploy_bot = build_deploy_bot()
        escalate_to_deploy = deploy_bot.as_tool(
            name="escalate_to_deploy",
            description=(
                "Escalate a restart or version deploy to the deploy bot — the ONLY "
                "way to run it (the release lead cannot deploy directly). Pass the "
                "service, and for a deploy the version, environment, and config. The "
                "deploy bot re-gates it under the caller's role, so it is not an "
                "escape hatch."
            ),
        )
        release_lead, _handler = create_agent(
            model="gpt-4o-mini",
            tools=[escalate_to_deploy],
            system_prompt=(
                "You are the release lead. You CANNOT restart or deploy services "
                "yourself — to do either, escalate to the deploy bot with "
                "escalate_to_deploy (pass the service, version, environment, config)."
            ),
            name="release_lead",
        )
        return release_lead

    def build_developer():
        """L1 — the only agent the user talks to. Reads status; escalates deploys."""

        @tool
        def service_status(service: str) -> str:
            """Look up the current status of a service."""
            return (
                f"(demo) {service}: v2.3.0 live in prod, v2.3.1 staged; "
                "last deploy 3h ago; health OK."
            )

        release_lead = build_release_lead()
        escalate_to_release = release_lead.as_tool(
            name="escalate_to_release",
            description=(
                "Escalate a restart or version deploy to the release lead — the ONLY "
                "path to one (the developer cannot deploy directly). Pass the service, "
                "and for a deploy the version, environment, and config. The release "
                "lead escalates to the deploy bot, which re-gates it under the "
                "caller's role."
            ),
        )
        # name MUST be developer: the platform gates the served root with the seeded
        # compose bundle for that agent in the showcase project.
        return create_agent(
            model="gpt-4o-mini",
            tools=[service_status, escalate_to_release],
            system_prompt=(
                "You are a developer assistant. You can check service status with "
                "service_status. You CANNOT restart or deploy services — to do "
                "either, escalate to the release lead with escalate_to_release (pass "
                "the service, and for a deploy the version, environment, and config)."
            ),
            name="developer",
        )

    return (build_developer,)


@app.cell
def _(mo):
    mo.md("""
    ## 1 · Start the agent

    Paste your OpenAI key and click **Start**. This builds the `developer` tree and
    serves it to the dashboard in the background (hot-swaps if you re-run).
    """)
    return


@app.cell
def _(mo):
    api_key = mo.ui.text(kind="password", placeholder="sk-...", full_width=True)
    start = mo.ui.run_button(label="▶ Start developer")
    mo.vstack([mo.md("**OpenAI API key**"), api_key, start])
    return api_key, start


@app.cell
def _(api_key, build_developer, mo, serve_manager, start, time):
    import os

    if start.value and api_key.value:
        os.environ["OPENAI_API_KEY"] = api_key.value
        _agent, _handler = build_developer()
        serve_manager.apply(_agent)
        time.sleep(3)
        _status = serve_manager.status()
        if _status == "running":
            _out = mo.callout(
                mo.md(
                    "✅ **developer is serving** (with its release_lead → deploy_bot "
                    "sub-agents). Open the dashboard above and chat in the Playground."
                ),
                kind="success",
            )
        else:
            _out = mo.callout(
                mo.md(f"serve status: `{_status}` — check the console."),
                kind="warn",
            )
    else:
        _out = mo.callout(
            mo.md("Enter your OpenAI key and click **▶ Start developer**."),
            kind="info",
        )
    _out
    return


@app.cell
def _(mo):
    mo.md("""
    ## 2 · The policy it enforces (compose)

    Each tier's role-aware policy is composed from `policy.yaml` + capability files
    (the same pipeline as `hexgate policy resolve --file policy.yaml`), then served by
    the platform. An `ingress` capability grants admission (who may start or be
    delegated to an agent), `escalate` grants the **reach** to the next tier
    (`agent.tool:release_lead` / `agent.tool:deploy_bot`), and the `staging`/`prod`
    caps grant the privileged tools on `deploy_bot` with a per-role env cap (prod
    version deploys are approval-gated). The tables below resolve all three agents —
    exactly what the served tree enforces.
    """)
    return


@app.cell
def _(Path, mo, resolve_file):
    import shutil
    import tempfile

    # The seeded showcase policy, inlined so this cell runs standalone. Kept in sync
    # with platform/api/.../policy_modules/devops_seed.py (DEVOPS_SEED_FILES).
    _FILES = {
        "policy.yaml": """\
boundary:
  tools:
    service_status: { mode: allow }    # safe read — any admitted seat
    restart_service: { mode: allow }   # privileged; granted only on deploy_bot, capped per role
    deploy_version: { mode: allow }    # privileged; granted only on deploy_bot (prod = change control)
  reach:
    release_lead: { as: tool }   # reach ceiling: developer may be granted agent-as-tool reach to release_lead
    deploy_bot: { as: tool }     # reach ceiling: release_lead may be granted agent-as-tool reach to deploy_bot
  admission: { mode: allow }    # ingress ceiling: a seat may be admitted to start / be delegated to an agent
agents:
  # L1 — the only agent the user talks to. Reads service status; it has NO deploy
  # tool, so a restart/deploy must escalate to release_lead (agent.tool:release_lead).
  developer:
    roles:
      # viewer may start developer and read status but NOT escalate (no reach) —
      # a deploy request dies here, at the first hop.
      viewer:
        import: [ caps/base/ingress.yaml, caps/base/status.yaml ]
      # engineer/release may start developer and escalate down the chain.
      engineer:
        import: [ caps/base/ingress.yaml, caps/base/status.yaml, caps/dev/escalate.yaml ]
      release:
        import: [ caps/base/ingress.yaml, caps/base/status.yaml, caps/dev/escalate.yaml ]
  # L2 — the release lead. Reached only as developer's agent-as-tool; its only move
  # is to escalate further to deploy_bot (reach edge agent.tool:deploy_bot).
  release_lead:
    roles:
      # Never reached by viewer (developer denies it the reach) and not admitted
      # here either — defense in depth.
      viewer: { import: [ caps/base/status.yaml ] }
      engineer:
        import: [ caps/base/ingress.yaml, caps/release/escalate.yaml ]
      release:
        import: [ caps/base/ingress.yaml, caps/release/escalate.yaml ]
  # L3 — the deploy bot that actually holds restart_service + deploy_version, three
  # hops from the user and reached only as release_lead's agent-as-tool. The caps
  # ride the delegating seat's role: engineer may act on STAGING only; release may
  # restart any env, and its version deploys are approval-gated (change control).
  deploy_bot:
    roles:
      viewer: { import: [ caps/base/status.yaml ] }
      engineer:
        import: [ caps/base/ingress.yaml, caps/deploy/staging.yaml ]
      release:
        import: [ caps/base/ingress.yaml, caps/deploy/prod.yaml ]
""",
        "caps/base/status.yaml": ("tools:\n  service_status: { mode: allow }\n"),
        "caps/base/ingress.yaml": ("admission:\n  mode: allow\n"),
        "caps/dev/escalate.yaml": ("reach:\n  release_lead: { as: tool }\n"),
        "caps/release/escalate.yaml": ("reach:\n  deploy_bot: { as: tool }\n"),
        "caps/deploy/staging.yaml": (
            "tools:\n"
            "  restart_service: { mode: allow, constraint: 'args.env == \"staging\"' }\n"
            "  deploy_version: { mode: allow, constraint: 'args.env == \"staging\"' }\n"
        ),
        "caps/deploy/prod.yaml": (
            "tools:\n"
            "  restart_service: { mode: allow }\n"
            "  deploy_version: { mode: approval_required }\n"
        ),
    }
    _root = Path(tempfile.gettempdir()) / "hexgate-devops-demo"
    shutil.rmtree(_root, ignore_errors=True)
    for _name, _body in _FILES.items():
        _p = _root / _name
        _p.parent.mkdir(parents=True, exist_ok=True)
        _p.write_text(_body, encoding="utf-8")

    _L = {"allow": "✅ allow", "deny": "❌ deny", "needs_approval": "🔶 approval"}

    def _cell(agent, role, tool, args):
        _ps = resolve_file(str(_root / "policy.yaml"), agent=agent).policy_set
        return _L.get(_ps.evaluate(role=role, tool=tool, args=args).outcome.value, "?")

    _roles = ["viewer", "engineer", "release"]

    # L1 developer: start + read + reach release_lead
    _d = [
        "| role | start `developer`?<br>`agent.run` | `service_status` | "
        "reach release_lead<br>`agent.tool:release_lead` |",
        "|---|---|---|---|",
    ]
    for _r in _roles:
        _d.append(
            f"| `{_r}` | {_cell('developer', _r, 'agent.run', {})} | "
            f"{_cell('developer', _r, 'service_status', {})} | "
            f"{_cell('developer', _r, 'agent.tool:release_lead', {})} |"
        )

    # L2 release_lead: delegated-to + reach deploy_bot
    _rl = [
        "| role | delegated-to?<br>`agent.run` | reach deploy_bot<br>`agent.tool:deploy_bot` |",
        "|---|---|---|",
    ]
    for _r in _roles:
        _rl.append(
            f"| `{_r}` | {_cell('release_lead', _r, 'agent.run', {})} | "
            f"{_cell('release_lead', _r, 'agent.tool:deploy_bot', {})} |"
        )

    # L3 deploy_bot: delegated-to + restart/deploy per env
    _db = [
        "| role | delegated-to?<br>`agent.run` | `restart_service`<br>staging / prod | "
        "`deploy_version`<br>staging / prod |",
        "|---|---|---|---|",
    ]
    for _r in _roles:
        _db.append(
            f"| `{_r}` | {_cell('deploy_bot', _r, 'agent.run', {})} | "
            f"{_cell('deploy_bot', _r, 'restart_service', {'env': 'staging'})} / "
            f"{_cell('deploy_bot', _r, 'restart_service', {'env': 'prod'})} | "
            f"{_cell('deploy_bot', _r, 'deploy_version', {'env': 'staging'})} / "
            f"{_cell('deploy_bot', _r, 'deploy_version', {'env': 'prod'})} |"
        )

    mo.md(
        "**L1 · developer** (the agent you talk to)\n\n"
        + "\n".join(_d)
        + "\n\n> `viewer` can start developer and read status, but the reach "
        "`agent.tool:release_lead` is **denied** — a deploy dies at the first hop.\n\n"
        "**L2 · release_lead** (reached only via developer)\n\n"
        + "\n".join(_rl)
        + "\n\n> `engineer`/`release` are admitted (delegated-to) and may reach "
        "`deploy_bot`; `viewer` never gets here.\n\n"
        "**L3 · deploy_bot** (reached only via release_lead — three hops, holds the tools)\n\n"
        + "\n".join(_db)
        + "\n\n> The privileged tools live only on `deploy_bot`, capped by the "
        "caller's role: `engineer` → **staging** only; `release` → restart **any** env "
        "and version deploys are **approval-gated** (change control, prod included). "
        "Every tier's policy is dashboard-editable."
    )
    return


@app.cell
def _(mo):
    mo.md("""
    ## 3 · Test it in the dashboard
    """)
    return


@app.cell
def _(Path, mo):
    _p = Path("/tmp/hexgate_dash_url")
    if _p.is_file():
        _dash = _p.read_text().strip().rstrip("/")
        _out = mo.md(
            f"### [▶ Open the dashboard →]({_dash}/v1/demo-login)\n\n"
            "In the Playground:\n\n"
            "1. Under **Acting as**, pick a role.\n"
            '2. Ask: *"Deploy checkout v2.3.1 to staging with the canary config."*\n'
            "3. As `viewer` → developer can read status, but the reach "
            "`agent.tool:release_lead` is **denied** in the Decisions sidebar — it "
            "can't escalate, so nothing deploys (the chain dies at the first hop).\n"
            "4. As `engineer` → developer escalates to **release_lead**, which "
            "escalates to **deploy_bot**, and deploy_bot deploys to **staging** — "
            "three reach edges, all allowed. Now ask to deploy to **prod**: the chain "
            "runs all the way to deploy_bot, which **denies** it (engineer is capped "
            "to staging).\n"
            "5. As `release` → a prod deploy runs the full chain and deploy_bot "
            "returns **needs approval** (change control) — approve it in the Playground "
            "and it rolls out; a `restart_service` on prod runs without approval.\n"
            "6. Edit any tier's policy in the **Policies** tab, live: revoke "
            "`release_lead`'s reach to `deploy_bot` (remove `caps/release/escalate.yaml` "
            "from its imports) and the chain breaks at the **second** hop; or give "
            "`engineer` the `caps/deploy/prod.yaml` cap and its prod deploy now goes "
            "through (with approval). The **Graph** tab shows the "
            "`developer → release_lead → deploy_bot` chain of `agent.tool:` edges."
        )
    else:
        _out = mo.callout(
            mo.md(
                "Dashboard URL not found. Launch this demo with `make demo-devops` "
                "— `boot.py` starts the platform + dashboard and writes "
                "`/tmp/hexgate_dash_url`."
            ),
            kind="info",
        )
    _out
    return


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
