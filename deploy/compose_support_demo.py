"""Hexgate compose support-bot demo (marimo) — a live agent, gated per role, in the dashboard.

The interactive half of the compose showcase. You define a real front-line
**support_bot** with a **billing_bot sub-agent** (reached with the shipped
`billing_bot.as_tool()` construct) and serve it to the dashboard from this notebook.

support_bot has **no refund tool** — refunds live only in billing_bot, reached as
an agent-as-tool; the user never touches billing_bot directly. Drive it in the
Playground:

  * as `default` — read-only and **not admitted to start the bot**: `agent.run` is
    denied at admission, so the bot never starts and nothing bills (the reach to
    billing_bot is never reached).
  * as `support` — admitted to start the bot; the reach to billing_bot is allowed,
    and billing_bot refunds up to **$200** for this seat (a bigger amount is denied
    *inside* the sub-agent). Delegation is the only refund path.
  * as `billing` — admitted; reaches billing_bot the same way, but it allows up to
    the **$1000** org ceiling (and this seat may also queue invoices, approval-gated).

What's new (the sub-agent-registration series + serve-time binding):
support_bot's policy gates the delegation as a **reach edge**
`agent.tool:billing_bot` — a per-role reach decision on the sub-agent, not a plain
tool name. billing_bot is its **own** compose agent: serve registers it and binds
its dashboard-editable platform policy (the serve path binds each sub-agent, not
just the root), so its per-role refund caps are edited in the Policies tab like any
other agent's — no in-kernel policy, no build-time credential.

support_bot's role-aware policy is the **compose** policy authored as
`policy.yaml` + capability files and seeded into the default (support-bot) project
(see `platform/api/.../policy_modules/seed_data.py`). The dashboard's Policies
editor shows and edits it; here you watch it gate a live agent.

Run with `make demo-support` — boot.py brings up the platform + dashboard scoped
to the showcase project; paste your OpenAI key here and click Start.
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
    # the notebook from an arbitrary CWD. NOTE: no HEXGATE_LOCAL_MODE here — the
    # served agent talks to the live platform (the resolved-policy table below is
    # pure resolution, which needs no platform).
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import serve_manager

    from hexgate.security.compose import resolve_file

    return Path, mo, resolve_file, serve_manager, time


@app.cell
def _(mo):
    mo.md("""
    # 🎧 Hexgate — a live support agent, gated by a *compose* policy

    **support_bot** is your front-line agent — the only one you talk to. It looks
    up orders and, when a refund is needed, **delegates to a `billing_bot`
    sub-agent** via `delegate_to_billing`. support_bot has **no refund tool of its
    own**: nobody refunds directly, and you have **no direct access to billing_bot**
    — you must go through support_bot.

    This shows the **sub-agent reach** capability: support_bot's policy gates the
    delegation as the **reach edge** `agent.tool:billing_bot` (a per-role reach
    decision on the sub-agent, not a plain tool name), while `billing_bot` — its own
    dashboard-editable compose agent — enforces its role-aware refund caps, plus a
    **global boundary** (the org's $1000 ceiling). `default` isn't even **admitted to
    start** the bot; `support` and `billing` start it and reach billing_bot;
    billing_bot caps a `support` delegation at **$200** and a `billing` one at
    **$1000**.

    support_bot's policy is the compose `policy.yaml` (+ capability files) seeded
    into the default project — the same one the dashboard's **Policies** editor
    shows. Here you serve support_bot and test it in the **Playground**. Paste your
    OpenAI key below (used only in this kernel).
    """)
    return


@app.cell
def _(Path, mo):
    # Under `make demo-support`, boot.py writes the running dashboard URL here and
    # /v1/demo-login signs you in. Standalone (`marimo edit`) there's no dashboard
    # — guard the read and show how to launch one.
    try:
        _dash = Path("/tmp/hexgate_dash_url").read_text().strip().rstrip("/")
    except OSError:
        _dash = None
    if _dash:
        _banner = mo.md(
            f"""
            ### [▶ Open the live dashboard →]({_dash}/v1/demo-login)

            Signs you in on the **plum** dashboard, scoped to the seeded
            default project. Start the agent below, then chat with it in
            the **Playground** — the Policies editor shows the very policy gating it.
            """
        )
    else:
        _banner = mo.callout(
            mo.md(
                "Launch the **platform + dashboard + this agent together** with "
                "`make demo-support` (→ http://localhost:2718) to chat in the live "
                "**Playground**. The policy table below also runs standalone."
            ),
            kind="info",
        )
    _banner
    return


@app.cell
def _():
    # -- Tools + agents --------------------------------------------------------
    # view_orders is a safe read (allowed for every role). support_bot has NO
    # refund_order tool — refunds happen only inside billing_bot, reached as an
    # agent-as-tool gated by the reach edge agent.tool:billing_bot: the default seat
    # is denied (not even admitted to start the bot); support and billing may reach
    # it, and billing_bot caps the refund by the caller's role ($200 vs $1000).
    from langchain_core.tools import tool

    from hexgate import create_agent

    # -- billing_bot: the SECOND agent support_bot reaches as a tool. It is its OWN
    # compose agent — serve_manager registers it (auto_register_subagents) and the
    # serve path binds its dashboard-editable platform policy — so we build it PLAIN
    # here, with no in-kernel enforcement. Its policy is role-aware: the delegating
    # seat's role rides the ambient HexgateContext into the nested run, so support is
    # capped at $200 and billing at the $1000 org ceiling; a role it doesn't grant
    # refunds nothing. Those caps live in billing_bot's compose block (seed_data.py /
    # _FILES below), editable in the dashboard's Policies tab.
    #
    # Posture: unlike the old in-kernel enforce_policy (caps held unconditionally),
    # the caps now depend on the serve path binding billing_bot's platform policy —
    # that's the point (dashboard-editable requires platform-bound). It's sound here:
    # serve_manager registers billing_bot (auto_register_subagents) and a registration
    # failure aborts serve loudly, so bind then finds it. A child that is never
    # registered would fail-soft to no policy, so keep billing_bot in the registered
    # tree.
    def build_billing():
        """Build the billing specialist sub-agent (plain — the serve path binds its
        platform policy; no build-time bind, so no credential is needed here)."""

        @tool
        def refund_order(order_id: str, amount: float) -> str:
            """Issue a refund against an order."""
            return f"(demo) refunded ${amount:.2f} on order {order_id}."

        billing, _handler = create_agent(
            model="gpt-4o-mini",
            tools=[refund_order],
            system_prompt=(
                "You are the billing specialist. Issue the requested refund with "
                "refund_order, confirming the order id and amount."
            ),
            name="billing_bot",
        )
        return billing

    @tool
    def view_orders(order_id: str) -> str:
        """Look up the current status of a customer's order."""
        return (
            f"(demo) Order {order_id}: shipped 2 days ago, total $128.40, "
            f"card ending 4242."
        )

    # NOTE: support_bot has NO refund_order tool. Refunds live ONLY in the
    # billing_bot sub-agent, mounted via `billing_bot.as_tool()` (the shipped
    # agent-as-tool construct) — no seat refunds directly. support_bot's policy gates
    # the delegation as the reach edge agent.tool:billing_bot; billing_bot enforces its
    # own role-aware caps from its bound platform policy.

    # delegate_to_billing is billing_bot mounted as an agent-as-tool via the shipped
    # `child.as_tool()` construct — no hand-written closure. On call, support_bot's
    # policy decides the REACH edge `agent.tool:billing_bot` (granted to support/billing,
    # denied to default — a per-role reach decision in the Decisions panel), then runs
    # billing_bot's OWN enforced ainvoke with the caller's role riding the ambient
    # context in — so billing_bot re-gates the refund ($200 support / $1000 billing).
    # Delegation is the only refund path and never an escape hatch. Built in
    # build_support (below), once the key is set — create_agent instantiates ChatOpenAI
    # eagerly.

    # MCP tools from the demo server (named mcp-<server>-<tool>). The gate keys on
    # the tool name, so these match the policy's mcp: grants: compute_tip is open,
    # send_invoice needs approval for billing, read_secret is denied outright.
    @tool("mcp-demo-compute_tip")
    def compute_tip(amount: float, percent: float = 18.0) -> str:
        """Compute the tip on a bill (MCP demo tool)."""
        return (
            f"(demo) tip on ${amount:.2f} at {percent}% = ${amount * percent / 100:.2f}"
        )

    @tool("mcp-demo-send_invoice")
    def send_invoice(order_id: str, amount: float) -> str:
        """Queue an invoice for an order (MCP demo tool; approval-gated)."""
        return f"(demo) invoice queued for order {order_id}: ${amount:.2f}"

    @tool("mcp-demo-read_secret")
    def read_secret(key: str) -> str:
        """Read a stored secret (MCP demo tool; policy-denied)."""
        return f"(demo) secret[{key}] = ****"

    def build_support():
        # name MUST be support_bot: the platform gates the served agent with the
        # seeded compose bundle for that agent in the default project.
        #
        # billing_bot is a first-class HexgateAgent mounted on support_bot with
        # `child.as_tool()` — the shipped agent-as-tool construct. support_bot's policy
        # gates the delegation as the reach edge `agent.tool:billing_bot`; billing_bot
        # enforces its own role-aware caps from its own (bound-at-serve) compose policy.
        billing_bot = build_billing()
        delegate_to_billing = billing_bot.as_tool(
            name="delegate_to_billing",
            description=(
                "Delegate a refund to the billing_bot sub-agent — the ONLY refund path, "
                "for every seat (support_bot cannot refund directly). Pass the order id, "
                "amount, and reason. billing_bot re-gates the refund under the caller's "
                "role, so it is not an escape hatch."
            ),
        )
        return create_agent(
            model="gpt-4o-mini",
            tools=[
                view_orders,
                delegate_to_billing,
                compute_tip,
                send_invoice,
                read_secret,
            ],
            system_prompt=(
                "You are a front-line customer support agent for an online store. "
                "Help customers check order status with view_orders. You CANNOT "
                "refund directly — for a refund, delegate it to billing with "
                "delegate_to_billing (order id, amount, reason)."
            ),
            name="support_bot",
        )

    return (build_support,)


@app.cell
def _(mo):
    mo.md("""
    ## 1 · Start the agent

    Paste your OpenAI key and click **Start**. This builds support_bot and serves
    it to the dashboard in the background (hot-swaps if you re-run).
    """)
    return


@app.cell
def _(mo):
    api_key = mo.ui.text(kind="password", placeholder="sk-...", full_width=True)
    start = mo.ui.run_button(label="▶ Start support_bot")
    mo.vstack([mo.md("**OpenAI API key**"), api_key, start])
    return api_key, start


@app.cell
def _(api_key, build_support, mo, serve_manager, start, time):
    import os

    if start.value and api_key.value:
        os.environ["OPENAI_API_KEY"] = api_key.value
        _agent, _handler = build_support()
        serve_manager.apply(_agent)
        time.sleep(3)
        _status = serve_manager.status()
        if _status == "running":
            _out = mo.callout(
                mo.md(
                    "✅ **support_bot is serving.** Open the dashboard above and "
                    "chat with it in the Playground."
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
            mo.md("Enter your OpenAI key and click **▶ Start support_bot**."),
            kind="info",
        )
    _out
    return


@app.cell
def _(mo):
    mo.md("""
    ## 2 · The policy it enforces (compose)

    support_bot's role-aware policy is composed from `policy.yaml` + capability
    files by the compose front-end (the same pipeline as
    `hexgate policy resolve --file policy.yaml`), then served by the platform. An
    `ingress` capability grants admission (who may start the bot), `desk` grants the
    support tools, `delegate` grants the **reach** to billing_bot
    (`agent.tool:billing_bot`), and `invoicing` grants the (approval-gated) invoice
    tool; each role imports only the capabilities it should have. support_bot grants
    `refund_order` to no seat — the refund lives in **billing_bot**, its own compose
    agent, which enforces its per-role caps from a policy the platform binds to it at
    serve (dashboard-editable, like support_bot's). The tables below resolve both
    agents' policies — exactly what the served agents enforce.
    """)
    return


@app.cell
def _(Path, mo, resolve_file):
    import shutil
    import tempfile

    # The seeded showcase policy, inlined so this cell runs standalone. Kept in
    # sync with platform/api/.../policy_modules/seed_data.py (SEED_POLICY_FILES).
    _FILES = {
        "policy.yaml": """\
boundary:
  tools:
    view_orders: { mode: allow }
    send_email: { mode: allow }
    escalate: { mode: allow }
    refund_order: { mode: allow, constraint: "args.amount <= 1000" }  # global org cap — declared, but granted to NO support_bot seat: refunds happen only inside billing_bot
    mcp-demo-compute_tip: { mode: allow }     # safe MCP tool
    mcp-demo-send_invoice: { mode: allow }    # ceiling; billing grants w/ approval
    mcp-demo-read_secret: { mode: deny }      # dangerous MCP tool — always denied
  reach:
    billing_bot: { as: tool }   # reach ceiling: support_bot may be granted agent-as-tool reach to billing_bot
  admission: { mode: allow }    # ingress ceiling: a seat may be admitted to start / be delegated to a bot
agents:
  # Front-line agent. Has NO refund_order tool — refunds happen only inside
  # billing_bot, reached as an agent-as-tool. The delegation is gated by the
  # REACH key agent.tool:billing_bot (not a plain tool name), so support_bot's
  # policy governs the sub-agent tool use per role.
  support_bot:
    roles:
      # The default seat browses read-only data but may NOT start the bot (no ingress).
      default: { import: [ caps/base/read_only.yaml ] }
      # The support seat starts the front-line bot and reaches billing_bot as a
      # tool (support_bot has no refund_order tool — nobody refunds direct).
      support:
        import:
          [ caps/base/read_only.yaml, caps/base/ingress.yaml,
            caps/support/desk.yaml, caps/support/delegate.yaml ]
      # The billing seat additionally may queue invoices (approval); it still
      # refunds only by delegating — billing_bot caps its delegation higher.
      billing:
        import:
          [ caps/base/read_only.yaml, caps/base/ingress.yaml,
            caps/support/desk.yaml, caps/billing/invoicing.yaml,
            caps/support/delegate.yaml ]
  # The billing specialist, reached only as support_bot's agent-as-tool (never
  # started directly — no seat has direct access). It is its OWN compose agent, so
  # the platform binds this policy to it at serve and it's dashboard-editable. Its
  # refund cap rides the delegating seat's role into the nested run: support up to
  # $200, billing up to the $1000 org ceiling, any other seat nothing.
  billing_bot:
    roles:
      # Not admitted and grants no refund — a delegated default seat bills nothing
      # (support_bot already denies default the reach; this is defense in depth).
      default: { import: [ caps/base/read_only.yaml ] }
      # A support delegation is admitted and refunds up to $200.
      support:
        import: [ caps/base/ingress.yaml, caps/billing/refund_support.yaml ]
      # A billing delegation is admitted and refunds up to the $1000 org ceiling.
      billing:
        import: [ caps/base/ingress.yaml, caps/billing/refund_billing.yaml ]
""",
        "caps/base/read_only.yaml": (
            "tools:\n  view_orders: { mode: allow }\n"
            "mcp:\n  mcp-demo-compute_tip: { mode: allow }\n"
        ),
        "caps/base/ingress.yaml": ("admission:\n  mode: allow\n"),
        "caps/support/desk.yaml": (
            "tools:\n"
            "  send_email: { mode: allow }\n"
            "  escalate: { mode: approval_required }\n"
        ),
        "caps/support/delegate.yaml": ("reach:\n  billing_bot: { as: tool }\n"),
        "caps/billing/invoicing.yaml": (
            "mcp:\n  mcp-demo-send_invoice: { mode: approval_required }\n"
        ),
        # billing_bot's refund grants (imported by its support/billing roles). Each cap
        # intersects with the boundary's $1000 org ceiling, so support lands at $200.
        "caps/billing/refund_support.yaml": (
            'tools:\n  refund_order: { mode: allow, constraint: "args.amount <= 200" }\n'
        ),
        "caps/billing/refund_billing.yaml": (
            'tools:\n  refund_order: { mode: allow, constraint: "args.amount <= 1000" }\n'
        ),
    }
    _root = Path(tempfile.gettempdir()) / "hexgate-compose-support-demo"
    shutil.rmtree(_root, ignore_errors=True)
    for _name, _body in _FILES.items():
        _p = _root / _name
        _p.parent.mkdir(parents=True, exist_ok=True)
        _p.write_text(_body, encoding="utf-8")

    # Resolve support_bot's compose policy — what the served agent enforces, incl.
    # the reach edge to billing_bot.
    _ps = resolve_file(str(_root / "policy.yaml"), agent="support_bot").policy_set
    _L = {"allow": "✅ allow", "deny": "❌ deny", "needs_approval": "🔶 approval"}

    def _cell(role, tool, args):
        return _L.get(_ps.evaluate(role=role, tool=tool, args=args).outcome.value, "?")

    _rows = [
        "| role | start `support_bot`?<br>`agent.run` | `view_orders` | "
        "`refund_order`<br>`$800 USD` | reach billing_bot<br>`agent.tool:billing_bot` |",
        "|---|---|---|---|---|",
    ]
    for _role in ["default", "support", "billing"]:
        _a = _cell(_role, "agent.run", {})
        _v = _cell(_role, "view_orders", {})
        _r = _cell(_role, "refund_order", {"amount": 800, "currency": "USD"})
        _reach = _cell(_role, "agent.tool:billing_bot", {})
        _rows.append(f"| `{_role}` | {_a} | {_v} | {_r} | {_reach} |")

    # Resolve billing_bot's OWN compose policy — the sub-agent's per-role refund
    # caps, bound to it at serve (dashboard-editable). The delegating seat's role
    # rides into the nested run, so this is the cap that actually gates a refund.
    _bps = resolve_file(str(_root / "policy.yaml"), agent="billing_bot").policy_set

    def _bcell(role, tool, args):
        return _L.get(_bps.evaluate(role=role, tool=tool, args=args).outcome.value, "?")

    _brows = [
        "| delegating role | delegated-to?<br>`agent.run` | "
        "`refund_order`<br>`$200` | `refund_order`<br>`$800` |",
        "|---|---|---|---|",
    ]
    for _role in ["default", "support", "billing"]:
        _a = _bcell(_role, "agent.run", {})
        _r200 = _bcell(_role, "refund_order", {"amount": 200, "currency": "USD"})
        _r800 = _bcell(_role, "refund_order", {"amount": 800, "currency": "USD"})
        _brows.append(f"| `{_role}` | {_a} | {_r200} | {_r800} |")

    mo.md(
        "**Resolved policy (what the served support_bot enforces)**\n\n"
        + "\n".join(_rows)
        + "\n\n> `default` can browse but **can't start the bot** (no admission). "
        "**No seat refunds directly** — `refund_order` isn't a support_bot tool, and "
        "the boundary declares it (the $1000 org cap) but grants it to no seat. Instead "
        "support_bot **reaches billing_bot as a tool**, gated by the reach edge "
        "`agent.tool:billing_bot` (granted to `support`/`billing`, denied to `default`) — "
        "a per-role **reach** decision, not a plain tool name. That's the new "
        "sub-agent-reach capability.\n\n"
        "**billing_bot's own resolved policy (bound to the sub-agent at serve)**\n\n"
        + "\n".join(_brows)
        + "\n\n> billing_bot enforces its **own** role-aware refund caps: the delegating "
        "seat's role rides into the nested run, so a **support** delegation is capped at "
        "**$200** (the $800 is denied) and a **billing** one at the **$1000** ceiling — "
        "delegation is not an escape hatch, and the user never talks to billing_bot "
        "directly. This policy is dashboard-editable, just like support_bot's."
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
            '2. Ask: *"Please refund order A-1001 for $40, it arrived damaged."*\n'
            "3. As `default` → the run is refused at **admission**: `agent.run` is "
            "**denied** in the Decisions sidebar and the bot never starts, so the model "
            "never runs and nothing bills (the reach to billing_bot is never reached).\n"
            "4. As `support` → the reach is **allowed** and runs the billing_bot "
            "sub-agent, which refunds the $40 — delegation is the only refund path. Now "
            "ask for **$500**: billing_bot's **own** policy **denies** it (a support "
            "delegation is capped at $200), so the escalated amount doesn't bill.\n"
            "5. As `billing` → the same delegation refunds up to **$1000** — the $500 "
            "now goes through.\n"
            "6. Edit either agent's policy in the **Policies** tab, live: revoke "
            "`support`'s reach on **support_bot** (remove `caps/support/delegate.yaml` "
            "from the `support` role's imports — don't delete the file, `billing` "
            "imports it too) and the next message denies the delegation; or raise "
            "**billing_bot**'s support cap (`caps/billing/refund_support.yaml`) and the "
            "$500 support refund now goes through — billing_bot is its own registered, "
            "editable agent. The **Graph** tab shows the `agent.tool:billing_bot` edge "
            "from support_bot to it."
        )
    else:
        _out = mo.callout(
            mo.md(
                "Dashboard URL not found. Launch this demo with `make demo-support` "
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


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
