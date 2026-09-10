"""Hexgate compose support-bot demo (marimo) — a live agent, gated per role, in the dashboard.

The interactive half of the compose showcase. You define a real front-line
**support_bot** with a **billing_bot sub-agent** (exposed as the
`delegate_to_billing` tool), serve it to the dashboard from this notebook, and
drive it in the Playground:

  * as `support` / `default` — a refund is refused: `refund_order` and
    `delegate_to_billing` are denied by policy, so the billing sub-agent never runs.
  * as `billing` — refunds up to the boundary's $1000 ceiling are allowed, and the
    delegation runs billing_bot.

support_bot's role-aware policy is the **compose** policy authored as
`policy.yaml` + capability files and seeded into the `policy-showcase` project
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

    **support_bot** is your front-line agent. It looks up orders, and — for a
    **billing** seat — refunds and hands a refund off to a **billing_bot**
    sub-agent via `delegate_to_billing`. Refund + delegation are **policy-gated
    by role**: `support`/`default` are refused before the sub-agent ever runs;
    only `billing` may refund (up to a $1000 ceiling) and delegate.

    That policy is the compose `policy.yaml` (+ capability files) seeded into the
    `policy-showcase` project — the same one the dashboard's **Policies** editor
    shows. Here you serve support_bot and test it in the **Playground**. Paste
    your OpenAI key below (used only in this kernel).
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
            `policy-showcase` project. Start the agent below, then chat with it in
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
    # view_orders is a safe read (allowed for every role). refund_order and
    # delegate_to_billing are gated: policy decides whether the caller's role may
    # invoke them at all — support/default are refused before the tool runs.
    from langchain_core.tools import tool

    from hexgate import create_agent

    # -- billing_bot: a SECOND agent, reached by support_bot AS A TOOL ---------
    # (agent-as-tool). The delegation surfaces as the delegate_to_billing tool
    # call, which the policy gates by role. Native create_agent has no
    # first-class handoff primitive that can be served to the dashboard, so the
    # sub-agent is wired as the tool that runs it.
    def build_billing():
        """Build the billing specialist sub-agent (its own tool + prompt)."""

        @tool
        def issue_refund(order_id: str, amount: float) -> str:
            """Issue a refund against an order."""
            return f"(demo) refunded ${amount:.2f} on order {order_id}."

        billing, _handler = create_agent(
            model="gpt-4o-mini",
            tools=[issue_refund],
            system_prompt=(
                "You are the billing specialist. Issue the requested refund with "
                "issue_refund, confirming the order id and amount."
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

    @tool
    def refund_order(order_id: str, amount: float, currency: str = "USD") -> str:
        """Refund a customer's order (billing seats only; capped by policy)."""
        return f"(demo) refunded {amount:.2f} {currency} on order {order_id}."

    # billing_bot is built on first delegation (after the key is set, since
    # create_agent instantiates ChatOpenAI eagerly) and cached here.
    _billing = {}

    @tool
    async def delegate_to_billing(order_id: str, amount: float, reason: str) -> str:
        """Delegate a refund to the billing_bot sub-agent.

        Billing seats only — support/default are denied by policy before this
        tool runs, so billing_bot never sees the request.
        """
        if "agent" not in _billing:
            _billing["agent"] = build_billing()
        result = await _billing["agent"].ainvoke(
            {
                "messages": [
                    (
                        "user",
                        f"Refund order {order_id} for ${amount:.2f}. Reason: {reason}",
                    )
                ]
            },
            {},
        )
        return result["messages"][-1].content

    # MCP tools from the demo server (named mcp-<server>-<tool>). The gate keys on
    # the tool name, so these match the policy's mcp: grants: compute_tip is open,
    # send_invoice needs approval for billing, read_secret is denied outright.
    @tool("mcp-demo-compute_tip")
    def compute_tip(amount: float, percent: float = 18.0) -> str:
        """Compute the tip on a bill (MCP demo tool)."""
        return f"(demo) tip on ${amount:.2f} at {percent}% = ${amount * percent / 100:.2f}"

    @tool("mcp-demo-send_invoice")
    def send_invoice(order_id: str, amount: float) -> str:
        """Queue an invoice for an order (MCP demo tool; approval-gated)."""
        return f"(demo) invoice queued for order {order_id}: ${amount:.2f}"

    @tool("mcp-demo-read_secret")
    def read_secret(key: str) -> str:
        """Read a stored secret (MCP demo tool; policy-denied)."""
        return f"(demo) secret[{key}] = ****"

    TOOLS = [
        view_orders,
        refund_order,
        delegate_to_billing,
        compute_tip,
        send_invoice,
        read_secret,
    ]

    def build_support():
        # name MUST be support_bot: the platform gates the served agent with the
        # seeded compose bundle for that agent in the policy-showcase project.
        return create_agent(
            model="gpt-4o-mini",
            tools=TOOLS,
            system_prompt=(
                "You are a front-line customer support agent for an online store. "
                "Help customers check order status with view_orders. For a refund, "
                "either refund it directly with refund_order or delegate it to "
                "billing with delegate_to_billing (order id, amount, reason)."
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
    `hexgate policy resolve --file policy.yaml`), then served by the platform. A
    `billing_desk` capability grants `delegate_to_billing` and `payments` grants
    `refund_order`; only the `billing` role imports them. The table below resolves
    that exact policy — it matches what the served agent enforces.
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
            '    refund_order: { mode: allow, constraint: "args.amount <= 1000" }  # hard cap\n'
            "    delegate_to_billing: { mode: allow }   # ceiling; needs a capability grant\n"
            "    mcp-demo-compute_tip: { mode: allow }     # safe MCP tool\n"
            "    mcp-demo-send_invoice: { mode: allow }    # ceiling; billing grants w/ approval\n"
            "    mcp-demo-read_secret: { mode: deny }      # dangerous MCP tool — always denied\n"
            "  reach:\n"
            "    billing_bot: { as: handoff }   # reach ceiling: hand-off only, never as-tool\n"
            "agents:\n"
            "  support_bot:\n"
            "    roles:\n"
            "      default: { import: [ caps/read_only.yaml ] }\n"
            "      support: { import: [ caps/read_only.yaml, caps/support_leaf.yaml ] }\n"
            "      billing:\n"
            "        import:\n"
            "          [ caps/read_only.yaml, caps/payments.yaml, caps/billing_desk.yaml,\n"
            "            caps/billing_reach.yaml ]\n"
            "  billing_bot:\n"
            "    roles:\n"
            "      billing: { import: [ caps/payments.yaml ] }\n"
        ),
        "caps/read_only.yaml": (
            "tools:\n  view_orders: { mode: allow }\n"
            "mcp:\n  mcp-demo-compute_tip: { mode: allow }\n"
        ),
        "caps/support_leaf.yaml": (
            "tools:\n"
            "  send_email: { mode: allow }\n"
            "  escalate: { mode: approval_required }\n"
        ),
        "caps/payments.yaml": (
            "tools:\n"
            '  refund_order: { mode: allow, constraint: '
            '\'args.currency in ["USD", "EUR"]\' }\n'
        ),
        "caps/billing_desk.yaml": (
            "tools:\n  delegate_to_billing: { mode: allow }\n"
            "mcp:\n  mcp-demo-send_invoice: { mode: approval_required }\n"
        ),
        "caps/billing_reach.yaml": "reach:\n  billing_bot: { as: handoff }\n",
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
        "| role | `view_orders` | `refund_order`<br>`$800 USD` | `delegate_to_billing` |",
        "|---|---|---|---|",
    ]
    for _role in ["default", "support", "billing"]:
        _v = _cell(_role, "view_orders", {})
        _r = _cell(_role, "refund_order", {"amount": 800, "currency": "USD"})
        _d = _cell(_role, "delegate_to_billing", {})
        _rows.append(f"| `{_role}` | {_v} | {_r} | {_d} |")
    mo.md(
        "**Resolved policy (what the served support_bot enforces)**\n\n"
        + "\n".join(_rows)
        + "\n\n> `billing` refunds up to the **$1000** ceiling and delegates; "
        "`support`/`default` get read-only."
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
            "3. As `support` / `default` → `refund_order` and `delegate_to_billing` "
            "are **denied** in the Decisions sidebar; the billing sub-agent never runs.\n"
            "4. Switch to `billing` → the refund is **allowed** (under $1000) and the "
            "delegation runs billing_bot.\n"
            "5. Edit `policy.yaml` in the **Policies** tab — the next message picks "
            "it up."
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
