"""Hexgate compose support-bot demo (marimo) — a live agent, gated per role, in the dashboard.

The interactive half of the compose showcase. You define a real front-line
**support_bot** with a **billing_bot sub-agent** (reached as the
`delegate_to_billing` tool), serve it to the dashboard from this notebook, and
drive it in the Playground:

  * as `default` — read-only and **not admitted to start the bot** (`agent.run`
    is denied); even if it ran, `refund_order` and `delegate_to_billing` are both
    denied, so nothing bills.
  * as `support` — admitted to start the bot; the front-line seat *can't refund
    itself*, but it MAY delegate: `refund_order` is denied while
    `delegate_to_billing` runs billing_bot. Delegation to the sub-agent is the
    only path to a refund.
  * as `billing` — admitted to start either bot; the elevated seat refunds
    directly, up to the boundary's $1000 ceiling (and may still delegate).

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

    **support_bot** is your front-line agent. It looks up orders and, when a
    refund is needed, **delegates to a `billing_bot` sub-agent** via
    `delegate_to_billing`. Both are **policy-gated by role**: `default` isn't even
    **admitted to start** the bot; the `support` seat starts it but *can't refund
    itself* and **must delegate** to billing_bot; only the `billing` seat refunds
    directly (up to a $1000 ceiling).

    That policy is the compose `policy.yaml` (+ capability files) seeded into the
    default project — the same one the dashboard's **Policies** editor
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
    # view_orders is a safe read (allowed for every role). refund_order and
    # delegate_to_billing are gated by role: the default seat is denied both; the
    # support seat may delegate (its only refund path) but not refund directly;
    # only the billing seat refunds directly.
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

        The support and billing seats may delegate (it's support's only path to a
        refund); the default seat is denied by policy before this tool runs.
        billing_bot is role-aware — the caller's role rides the context into the
        nested run — so a support delegation is capped at $200 and a billing one
        at the $1000 org ceiling. Delegation isn't an escape hatch.
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
        # seeded compose bundle for that agent in the default project.
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
    `hexgate policy resolve --file policy.yaml`), then served by the platform. An
    `ingress` capability grants admission (who may start the bot), `delegate`
    grants `delegate_to_billing`, and `payments` grants `refund_order`; each role
    imports only the capabilities it should have. The table below resolves that
    exact policy — it matches what the served agent enforces.
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
            '    refund_order: { mode: allow, constraint: "args.amount <= 1000" }  # org hard cap\n'
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
            "      # The support seat starts the front-line bot; it can't refund itself, so it\n"
            "      # MUST delegate to billing_bot.\n"
            "      support:\n"
            "        import:\n"
            "          [ caps/base/read_only.yaml, caps/base/ingress.yaml,\n"
            "            caps/support/desk.yaml, caps/support/delegate.yaml ]\n"
            "      # The billing seat starts the bot, refunds directly, and may still delegate.\n"
            "      billing:\n"
            "        import:\n"
            "          [ caps/base/read_only.yaml, caps/base/ingress.yaml,\n"
            "            caps/support/desk.yaml, caps/billing/payments.yaml,\n"
            "            caps/billing/invoicing.yaml, caps/support/delegate.yaml ]\n"
            "  billing_bot:\n"
            "    roles:\n"
            "      # Only the billing seat may start the refunds specialist directly.\n"
            "      billing:\n"
            "        import:\n"
            "          [ caps/base/ingress.yaml, caps/billing/payments.yaml,\n"
            "            caps/billing/invoicing.yaml ]\n"
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
        "caps/billing/payments.yaml": (
            "tools:\n"
            "  refund_order: { mode: allow, constraint: "
            '\'args.currency in ["USD", "EUR"]\' }\n'
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
        + "\n\n> `default` can browse but **can't start the bot** (no admission); "
        "`support` starts it but can't refund directly — it **must "
        "`delegate_to_billing`** (the sub-agent); `billing` starts it and refunds "
        "directly up to the **$1000** ceiling. `billing_bot` is stricter still — "
        "only the `billing` seat may start it directly (the `support` seat "
        "delegates to it via the tool, but can't start it head-on). `billing_bot` "
        "runs in-kernel and **enforces its own role-aware policy**: the caller's "
        "role rides the context into the nested run, so a **support** delegation "
        "is capped at **$200** and a **billing** one at **$1000** — delegation is "
        "not an escape hatch."
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
            "3. As `default` → `refund_order` and `delegate_to_billing` are both "
            "**denied** in the Decisions sidebar; nothing bills.\n"
            "4. As `support` → the direct `refund_order` is **denied**, but "
            "`delegate_to_billing` is **allowed** and runs the billing_bot sub-agent — "
            "delegation is the only way to refund from this seat.\n"
            "5. As `billing` → the refund is **allowed** directly (under $1000).\n"
            "6. Edit `policy.yaml` (or a `caps/…` file) in the **Policies** tab — the "
            "next message picks it up. The **Graph** tab shows support_bot's "
            "`delegate_to_billing` tool and each seat's admission edges to the bots."
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
