"""Hexgate compose support-bot demo (marimo) — one modular policy, tools *and* agents.

A read-top-to-bottom tour of a small customer-support system whose WHOLE policy —
tool permissions *and* agent-to-agent reach — is authored in the **compose**
front-end (`policy.yaml` + imported capability files) and enforced. Fully local
(no platform, no API key, no model call):

  policy.yaml + caps/*.yaml  →  compose.resolve_file(agent=…)  →  a PolicySet per role

Two agents: a front-line `support_bot` and a refunds specialist `billing_bot`. A
role's policy governs what tools it may call (support caps out before refunds;
billing refunds up to the boundary's $1000 ceiling) *and* whether `support_bot`
may hand the conversation off to `billing_bot`. Reach is closed-world: no role may
reach `billing_bot` until a capability grants it — the same import pipeline as
tools.

Run with `uv run --with marimo marimo edit deploy/compose_support_demo.py`.
"""

import marimo

__generated_with = "0.23.10"
app = marimo.App(width="medium")


@app.cell
def _():
    import os
    import shutil
    import tempfile
    from pathlib import Path

    # Force offline: the gate's enforcer wires an audit sender that otherwise
    # falls back to HEXGATE_API_KEY. This is a policy tour — keep it local.
    os.environ["HEXGATE_LOCAL_MODE"] = "1"

    import marimo as mo

    from hexgate import HexgateContext
    from hexgate.security import ReachNotAllowedError, resolve_reach_gate
    from hexgate.security.compose import resolve_file
    from hexgate.security.enforcer import build_enforcer

    return (
        HexgateContext,
        Path,
        ReachNotAllowedError,
        build_enforcer,
        mo,
        resolve_file,
        resolve_reach_gate,
        shutil,
        tempfile,
    )


@app.cell
def _(mo):
    mo.md(
        """
        # 🎧 Hexgate — one *compose* policy, tools *and* agents

        A customer-support system with two agents: front-line `support_bot` and
        refunds specialist `billing_bot`. Everything a role may do is composed from
        one **`policy.yaml`** that imports small **capability files**:

        - **Tool permissions** — may this role call this tool with these args?
        - **Agent reach** — may `support_bot` hand the conversation off to
          `billing_bot`?

        A top-level **boundary** sets the closed-world ceiling (allowed tools, the
        $1000 refund cap, and that reach to `billing_bot` is `handoff`-only). Each
        agent's roles `import:` the capability files they're granted. One
        `resolve_file` per agent folds it into a `PolicySet` per role — tools and
        reach on the same pipeline. All local: no platform, no API key, no model
        call.
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        """
        ## 1 · The policy — a `policy.yaml` that imports capabilities

        Edit any of these and the whole notebook re-resolves. The boundary is a
        ceiling (default deny): a tool — or a reach target — it doesn't list is
        ineligible no matter what a capability grants.
        """
    )
    return


@app.cell
def _():
    # The entry file: the closed-world boundary + two agents whose roles import
    # capability files. `import:` at a role splices that leaf file into the role.
    ENTRY = """\
boundary:
  tools:
    view_orders: { mode: allow }
    send_email: { mode: allow }
    escalate: { mode: allow }
    refund_order: { mode: allow, constraint: "args.amount <= 1000" }  # hard cap
  reach:
    billing_bot: { as: handoff }   # reach ceiling: hand-off only, never as-tool
agents:
  support_bot:
    roles:
      default: { import: [ caps/read_only.yaml ] }
      support: { import: [ caps/read_only.yaml, caps/support_leaf.yaml ] }
      billing:
        import:
          [ caps/read_only.yaml, caps/payments.yaml, caps/billing_reach.yaml ]
  billing_bot:
    roles:
      billing: { import: [ caps/payments.yaml ] }
"""
    # Leaf capability files — grant-only, imported by the roles above.
    CAPS = {
        "read_only.yaml": "tools:\n  view_orders: { mode: allow }\n",
        "support_leaf.yaml": (
            "tools:\n"
            "  send_email: { mode: allow }\n"
            "  escalate: { mode: approval_required }\n"
        ),
        "payments.yaml": (
            "tools:\n"
            '  refund_order: { mode: allow, constraint: '
            '\'args.currency in ["USD", "EUR"]\' }\n'
        ),
        # Grants the agent-level reach: this role's support_bot may hand off to
        # billing_bot. No grant -> closed-world deny.
        "billing_reach.yaml": "reach:\n  billing_bot: { as: handoff }\n",
    }
    return CAPS, ENTRY


@app.cell
def _(CAPS, ENTRY, Path, mo, shutil, tempfile):
    # Write the entry + caps to a policy tree and resolve through the real compose
    # loader — the same path as `hexgate policy resolve --file policy.yaml`. A
    # stable dir (recreated each run) so re-running on an edit doesn't leak.
    def _write_policy():
        root = Path(tempfile.gettempdir()) / "hexgate-compose-support-demo"
        shutil.rmtree(root, ignore_errors=True)
        (root / "caps").mkdir(parents=True, exist_ok=True)
        (root / "policy.yaml").write_text(ENTRY, encoding="utf-8")
        for name, body in CAPS.items():
            (root / "caps" / name).write_text(body, encoding="utf-8")
        return root

    POLICY_ROOT = _write_policy()
    mo.md(f"Policy written to `{POLICY_ROOT}` — entry `policy.yaml` + `caps/`.")
    return (POLICY_ROOT,)


@app.cell
def _(POLICY_ROOT, resolve_file):
    # One resolve per agent. Each agent's role-keyed PolicySet carries tools AND
    # agent reach — the fold lowers the imported `reach` blocks to
    # `agent.handoff:<target>` keys and composes them exactly like tool keys.
    def _resolve(agent):
        return resolve_file(str(POLICY_ROOT / "policy.yaml"), agent=agent).policy_set

    support_policy = _resolve("support_bot")
    billing_policy = _resolve("billing_bot")
    return billing_policy, support_policy


@app.cell
def _(mo, support_policy):
    _LABEL = {"allow": "✅", "deny": "❌", "needs_approval": "🔶 approval"}
    _CALLS = [
        ("view_orders", {}),
        ("send_email", {}),
        ("escalate", {}),
        ("refund_order", {"amount": 800, "currency": "USD"}),
        ("refund_order", {"amount": 2000, "currency": "USD"}),
    ]
    _ROLES = ["default", "support", "billing"]

    def _cell(role, tool, args):
        o = support_policy.evaluate(role=role, tool=tool, args=args).outcome.value
        return _LABEL.get(o, o)

    _cols = [f"`{t}`" + (f"<br>`{a}`" if a else "") for t, a in _CALLS]
    _header = "| role | " + " | ".join(_cols) + " |"
    _sep = "|" + "---|" * (len(_CALLS) + 1)
    _rows = [
        "| `" + r + "` | " + " | ".join(_cell(r, t, a) for t, a in _CALLS) + " |"
        for r in _ROLES
    ]
    mo.md(
        "**`support_bot` resolved tool policy, per role**\n\n"
        + "\n".join([_header, _sep, *_rows])
        + "\n\n> `support` sends email and escalates (with approval) but can't "
        "refund; `billing` refunds up to the boundary's **$1000** ceiling "
        "(`$2000 → deny`) and only in USD/EUR."
    )
    return


@app.cell
def _(mo):
    mo.md(
        """
        ## 2 · Agent reach — composed from the same imports

        Reach is **closed-world**: no role may reach `billing_bot` until a
        capability grants it. Only `billing` imports `caps/billing_reach.yaml`, so
        only `billing` may hand `support_bot`'s conversation off — `support` and
        `default` are denied, and nobody may reach it *as a tool* (the boundary
        ceilinged `handoff` only). This runs the real `ReachGate` — the seam the
        framework calls at a hand-off — over the composed policy.
        """
    )
    return


@app.cell
def _(
    HexgateContext,
    ReachNotAllowedError,
    build_enforcer,
    resolve_reach_gate,
    support_policy,
):
    def reach(role, via="handoff", target="billing_bot"):
        """Run the real ReachGate for `role`: may support_bot reach `target`?

        Returns (outcome, reason). The source agent's policy governs reach, so the
        gate is built from support_bot's enforcer and decides the target's lowered
        key. Closed-world denies an ungranted role."""
        gate = resolve_reach_gate(
            build_enforcer(support_policy, agent_name="support_bot")
        )
        roles = [role] if role is not None else []
        with HexgateContext(user_id="demo", user_roles=roles).sync_scope():
            try:
                gate.check_reach(target, via=via)
                return "allow", ""
            except ReachNotAllowedError as exc:
                return exc.decision.outcome.value, exc.decision.reason

    return (reach,)


@app.cell
def _(mo, reach):
    _LABEL = {"allow": "✅ allowed", "deny": "❌ refused", "needs_approval": "🔶 approval"}
    _rows = [
        "| caller role | `support_bot` → `billing_bot` (handoff) | (as tool) |",
        "|---|---|---|",
    ]
    for _role in ["default", "support", "billing"]:
        _h, _ = reach(_role, via="handoff")
        _t, _ = reach(_role, via="tool")
        _rows.append(f"| `{_role}` | {_LABEL.get(_h, _h)} | {_LABEL.get(_t, _t)} |")
    mo.md(
        "**Reach, live from the gate**\n\n"
        + "\n".join(_rows)
        + "\n\n> `billing` was granted `handoff` by `caps/billing_reach.yaml`; "
        "`support`/`default` hit the closed-world deny; and *as-tool* reach is "
        "refused for everyone (the boundary ceilinged `handoff` only)."
    )
    return


@app.cell
def _(mo):
    mo.md(
        """
        ## 3 · Test the agent

        Pick a caller **role**, a **tool** + args, and a **reach** target — the same
        composed policy decides both the tool call and the hand-off, live.
        """
    )
    return


@app.cell
def _(mo):
    probe = (
        mo.md(
            """
            **role** {role} &nbsp; **tool** {tool} &nbsp; **amount** {amount}
            &nbsp; **currency** {currency} &nbsp; **reach via** {via}
            """
        )
        .batch(
            role=mo.ui.dropdown(
                options=["default", "support", "billing"], value="support"
            ),
            tool=mo.ui.dropdown(
                options=["view_orders", "send_email", "escalate", "refund_order"],
                value="refund_order",
            ),
            amount=mo.ui.number(start=0, stop=5000, value=800),
            currency=mo.ui.dropdown(options=["USD", "EUR", "GBP"], value="USD"),
            via=mo.ui.dropdown(options=["handoff", "tool"], value="handoff"),
        )
        .form(submit_button_label="▶ Test the agent")
    )
    probe
    return (probe,)


@app.cell
def _(mo, probe, reach, support_policy):
    _LBL = {"allow": ("✅", "success"), "deny": ("❌", "danger"),
            "needs_approval": ("🔶", "warn")}
    if probe.value is None:
        _out = mo.callout(
            mo.md("Pick a role + tool, then **▶ Test the agent**."), kind="info"
        )
    else:
        _role = probe.value["role"]
        _tool = probe.value["tool"]
        _args = {"amount": probe.value["amount"], "currency": probe.value["currency"]}
        _outcome = support_policy.evaluate(
            role=_role, tool=_tool, args=_args
        ).outcome.value
        _icon, _kind = _LBL.get(_outcome, ("•", "info"))
        _tool_md = mo.callout(
            mo.md(
                f"{_icon} **`{_tool}`** as `{_role}` "
                f"(amount={_args['amount']}, {_args['currency']}) → **{_outcome}**"
            ),
            kind=_kind,
        )
        _via = probe.value["via"]
        _r_out, _r_reason = reach(_role, via=_via)
        _r_icon, _r_kind = _LBL.get(_r_out, ("•", "info"))
        _detail = f" — {_r_reason}" if _r_reason else ""
        _reach_md = mo.callout(
            mo.md(
                f"{_r_icon} reach `{_via}` → `billing_bot` as `{_role}` → "
                f"**{_r_out}**{_detail}"
            ),
            kind=_r_kind,
        )
        _out = mo.vstack([_tool_md, _reach_md])
    _out
    return


@app.cell
def _(billing_policy, mo):
    # billing_bot has its own, narrower policy: it only imports payments, so it
    # refunds (up to the ceiling) and nothing else.
    _r = billing_policy.evaluate(
        role="billing", tool="refund_order", args={"amount": 800, "currency": "USD"}
    ).outcome.value
    _v = billing_policy.evaluate(role="billing", tool="view_orders", args={}).outcome.value
    mo.md(
        "**`billing_bot`'s own policy** (it imports only `caps/payments.yaml`):\n\n"
        f"- `refund_order($800, USD)` → **{_r}**\n"
        f"- `view_orders` → **{_v}** (never granted to billing_bot)"
    )
    return


@app.cell
def _(mo):
    mo.md(
        """
        ---
        The same compose front-end powers the CLI and the dashboard — point the CLI
        at your own entry file:

        ```
        hexgate policy resolve --file policy.yaml --agent support_bot
        ```

        In the platform, these exact files are the project's `policy_file` rows: the
        **Policies** editor renders this scenario with a live resolved view, the
        decision tester, and the reach/admission graph. Reach and admission are
        enforced at run entry / hand-off by the `ReachGate` / `AgentGate` seams a
        live agent runs, over the policy the compose front-end composed.
        """
    )
    return


if __name__ == "__main__":
    app.run()
