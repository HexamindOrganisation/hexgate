"""Hexgate compose support-bot demo (marimo) — a live agent, gated per role, in the dashboard.

The interactive half of the compose showcase. You define a real front-line
**support_bot** with a **billing_bot sub-agent** (reached as the
`delegate_to_billing` tool) and serve it to the dashboard from this notebook.

support_bot has **no refund tool** — refunds live only in billing_bot, reached by
`delegate_to_billing`; the user never touches billing_bot directly. Drive it in
the Playground:

  * as `default` — read-only and **not admitted to start the bot** (`agent.run`
    is denied); `delegate_to_billing` is denied too, so nothing bills.
  * as `support` — admitted to start the bot; it delegates to billing_bot, which
    refunds up to **$200** for this seat (a bigger amount is denied *inside* the
    sub-agent). Delegation is the only refund path.
  * as `billing` — admitted; delegates the same way, but billing_bot allows up to
    the **$1000** org ceiling (and this seat may also queue invoices, approval-gated).

What's shipped: an **agent-level block** on the first-level agent (support_bot) +
a **global boundary** (the $1000 refund ceiling), with billing_bot **self-enforcing
its own role-aware policy in-kernel**. Per-sub-agent *dashboard* policy is the
sub-agent-registration series (PRs #233/#243/#244/#245), landing next.

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

    This shows what's **shipped today**: an **agent-level policy block** on the
    first-level agent + a **global boundary** (the org's $1000 refund ceiling),
    while the `billing_bot` sub-agent **self-enforces its own role-aware policy
    in-kernel**. `default` isn't even **admitted to start** the bot; `support` and
    `billing` start it and delegate; billing_bot caps a `support` delegation at
    **$200** and a `billing` one at **$1000**.

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
    # refund_order tool — refunds happen only inside billing_bot, reached via the
    # delegate_to_billing tool, which is gated by role: the default seat is denied
    # (not even admitted to start the bot); support and billing may delegate, and
    # billing_bot caps the refund by the caller's role ($200 vs $1000).
    from langchain_core.tools import tool

    from hexgate import create_agent

    # -- billing_bot: a SECOND agent that support_bot delegates to via the
    # delegate_to_billing tool. It runs IN-KERNEL (not served by the platform),
    # so it must enforce its OWN policy via enforce_policy — otherwise the
    # delegated refund would bypass the gate entirely.
    #
    # It's ROLE-AWARE: the caller's role rides the ambient HexgateContext into the
    # nested run, so billing_bot re-uses the delegating seat's role. A support-seat
    # delegation is capped tighter ($200 — the seat can't refund directly, so its
    # delegated refund is the smaller path) than a billing-seat one ($1000, the
    # org ceiling); a caller whose role billing_bot doesn't grant refunds nothing.
    #
    # NOTE: these are classic (non-compose) role policies loaded by the SDK
    # directly (one file per role in a policies/ dir), so they use the plural
    # `constraints: [...]` list — NOT the singular `constraint:` compose alias the
    # boundary/caps above use. The classic loader rejects `constraint:`.
    _BILLING_POLICIES = {
        # A caller with no matching role: billing_bot refunds nothing.
        "default": "default_policy: { mode: deny }\n",
        "support": (
            "default_policy: { mode: deny }\n"
            "tools:\n"
            '  refund_order: { mode: allow, constraints: ["args.amount <= 200"] }\n'
        ),
        "billing": (
            "default_policy: { mode: deny }\n"
            "tools:\n"
            '  refund_order: { mode: allow, constraints: ["args.amount <= 1000"] }\n'
        ),
    }

    def build_billing():
        """Build the billing specialist sub-agent, gated by its own role policy."""
        import os
        import tempfile

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
        # Role-keyed policy dir: one file per role, the stem is the role name.
        # enforce_policy loads + freezes the bundle here, so the temp dir can be
        # torn down right after — a private dir (no symlink/race), no leak.
        with tempfile.TemporaryDirectory() as _tmp:
            _dir = os.path.join(_tmp, "policies")
            os.makedirs(_dir)
            for _role, _pol in _BILLING_POLICIES.items():
                _path = os.path.join(_dir, f"{_role}.yaml")
                with open(_path, "w", encoding="utf-8") as _f:
                    _f.write(_pol)
            # Gate 1 enforcement on the in-kernel sub-agent, keyed on the role.
            return billing.enforce_policy(_dir)

    @tool
    def view_orders(order_id: str) -> str:
        """Look up the current status of a customer's order."""
        return (
            f"(demo) Order {order_id}: shipped 2 days ago, total $128.40, "
            f"card ending 4242."
        )

    # NOTE: support_bot has NO refund_order tool. Refunds live ONLY in the
    # billing_bot sub-agent, mounted via `billing_bot.as_tool()` (the shipped
    # agent-as-tool construct) — no seat refunds directly. (The org boundary still
    # declares a refund ceiling; see the policy cell. billing_bot self-enforces its own
    # policy in-kernel here; registering it as its own dashboard-editable agent —
    # `register_tree` — is the natural next step.)

    # delegate_to_billing is billing_bot mounted as an agent-as-tool via the shipped
    # `child.as_tool()` construct — no hand-written closure. On call it decides
    # `delegate_to_billing` under support_bot's policy (the seed bundle name-gates it:
    # default seat denied, support/billing allowed), then runs billing_bot's OWN enforced
    # ainvoke with the caller's role riding the ambient context in — so billing_bot
    # re-gates the refund ($200 for support, $1000 for billing). Delegation is the only
    # refund path and never an escape hatch. Built in build_support (below), once the key
    # is set — create_agent instantiates ChatOpenAI eagerly.

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
        # billing_bot is a first-class HexgateAgent (built + policy-enforced), mounted
        # on support_bot with `child.as_tool()` — the shipped agent-as-tool construct.
        # The delegation tool keeps the name `delegate_to_billing` so support_bot's seed
        # policy (which gates that tool name by role) governs the delegation unchanged.
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
    `ingress` capability grants admission (who may start the bot), `desk` grants
    the support tools, `delegate` grants `delegate_to_billing`, and `invoicing`
    grants the (approval-gated) invoice tool; each role imports only the
    capabilities it should have. No role grants `refund_order` — the boundary
    keeps it as the org ceiling, but the refund itself lives in billing_bot. The
    table below resolves that exact policy — it matches what the served agent
    enforces.
    """)
    return


@app.cell
def _(Path, mo, resolve_file):
    import shutil
    import tempfile

    # The seeded showcase policy, inlined so this cell runs standalone. Kept in
    # sync with platform/api/.../policy_modules/seed_data.py (SEED_POLICY_FILES).
    _FILES = {
        "policy.yaml": (
            "boundary:\n"
            "  tools:\n"
            "    view_orders: { mode: allow }\n"
            "    send_email: { mode: allow }\n"
            "    escalate: { mode: allow }\n"
            '    refund_order: { mode: allow, constraint: "args.amount <= 1000" }  # global org cap — declared, but granted to NO support_bot seat: refunds happen only inside billing_bot\n'
            "    delegate_to_billing: { mode: allow }   # ceiling; a capability grant activates it\n"
            "    mcp-demo-compute_tip: { mode: allow }     # safe MCP tool\n"
            "    mcp-demo-send_invoice: { mode: allow }    # ceiling; billing grants w/ approval\n"
            "    mcp-demo-read_secret: { mode: deny }      # dangerous MCP tool — always denied\n"
            "  admission: { mode: allow }    # ingress ceiling: a seat may be admitted to start a bot\n"
            "agents:\n"
            "  support_bot:\n"
            "    roles:\n"
            "      # The default seat browses read-only data but may NOT start the bot (no ingress).\n"
            "      default: { import: [ caps/base/read_only.yaml ] }\n"
            "      # The support seat starts the front-line bot and delegates refunds to\n"
            "      # billing_bot (support_bot has no refund_order tool — nobody refunds direct).\n"
            "      support:\n"
            "        import:\n"
            "          [ caps/base/read_only.yaml, caps/base/ingress.yaml,\n"
            "            caps/support/desk.yaml, caps/support/delegate.yaml ]\n"
            "      # The billing seat additionally may queue invoices (approval); it still\n"
            "      # refunds only by delegating — billing_bot caps its delegation higher.\n"
            "      billing:\n"
            "        import:\n"
            "          [ caps/base/read_only.yaml, caps/base/ingress.yaml,\n"
            "            caps/support/desk.yaml, caps/billing/invoicing.yaml,\n"
            "            caps/support/delegate.yaml ]\n"
        ),
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
        "caps/support/delegate.yaml": (
            "tools:\n  delegate_to_billing: { mode: allow }\n"
        ),
        "caps/billing/invoicing.yaml": (
            "mcp:\n  mcp-demo-send_invoice: { mode: approval_required }\n"
        ),
    }
    _root = Path(tempfile.gettempdir()) / "hexgate-compose-support-demo"
    shutil.rmtree(_root, ignore_errors=True)
    for _name, _body in _FILES.items():
        _p = _root / _name
        _p.parent.mkdir(parents=True, exist_ok=True)
        _p.write_text(_body, encoding="utf-8")

    _ps = resolve_file(str(_root / "policy.yaml"), agent="support_bot").policy_set
    _L = {"allow": "✅ allow", "deny": "❌ deny", "needs_approval": "🔶 approval"}

    def _cell(role, tool, args):
        return _L.get(
            _ps.evaluate(role=role, tool=tool, args=args).outcome.value,
            "?",
        )

    _rows = [
        "| role | start `support_bot`?<br>`agent.run` | `view_orders` | "
        "`refund_order`<br>`$800 USD` | `delegate_to_billing` |",
        "|---|---|---|---|---|",
    ]
    for _role in ["default", "support", "billing"]:
        _a = _cell(_role, "agent.run", {})
        _v = _cell(_role, "view_orders", {})
        _r = _cell(_role, "refund_order", {"amount": 800, "currency": "USD"})
        _d = _cell(_role, "delegate_to_billing", {})
        _rows.append(f"| `{_role}` | {_a} | {_v} | {_r} | {_d} |")
    mo.md(
        "**Resolved policy (what the served support_bot enforces)**\n\n"
        + "\n".join(_rows)
        + "\n\n> `default` can browse but **can't start the bot** (no admission). "
        "**No seat refunds directly** — `refund_order` isn't a support_bot tool, and "
        "the boundary declares it (the **$1000** org cap) but grants it to no seat, so "
        "it resolves ❌ for everyone. Refunds happen **only inside billing_bot**, reached "
        "by `delegate_to_billing` (which `support` and `billing` may call, `default` "
        "may not). billing_bot runs **in-kernel** and enforces its **own** role-aware "
        "policy: the caller's role rides the context into the nested run, so a "
        "**support** delegation is capped at **$200** and a **billing** one at the "
        "**$1000** org ceiling — delegation is not an escape hatch, and the user never "
        "talks to billing_bot directly.\n\n"
        "> The delegation uses the shipped **agent-as-tool** construct — "
        "`billing_bot.as_tool()` (PRs #233/#243/#244/#245) — so billing_bot is a "
        "first-class sub-agent gated by support_bot's policy on the way in and "
        "self-enforcing its own role-aware policy on the way through. It runs in-kernel "
        "here; registering it as its own **dashboard-editable** agent (`register_tree`) "
        "is the natural next step."
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
            "3. As `default` → `delegate_to_billing` is **denied** in the Decisions "
            "sidebar (the seat isn't even admitted to start the bot); nothing bills.\n"
            "4. As `support` → `delegate_to_billing` is **allowed** and runs the "
            "billing_bot sub-agent, which refunds the $40 — delegation is the only "
            "refund path. Now ask for **$500**: billing_bot **denies** it (a support "
            "delegation is capped at $200), so the escalated amount doesn't bill.\n"
            "5. As `billing` → the same delegation refunds up to **$1000** — the $500 "
            "now goes through.\n"
            "6. Edit `policy.yaml` (or a `caps/…` file) in the **Policies** tab — the "
            "next message picks it up. The **Graph** tab shows support_bot's "
            "`delegate_to_billing` tool and each seat's admission edge to support_bot."
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
