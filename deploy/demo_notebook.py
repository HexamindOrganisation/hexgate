"""Hexgate live-demo notebook (marimo) — define tools + agent in real cells.

The landing UI for a disposable demo container. The visitor pastes their own
OpenAI key, defines tools and the agent as real Python in cells, clicks
**Apply & reload**, and that live `agent` object is served to the dashboard
playground (serve_manager runs the serve loop in-kernel, bound to the object).

BYOK: the key is the visitor's own, lives only in this throwaway container
(one visitor per container, dies after idle), and is never logged.

Run by boot.py via `marimo edit`.
"""

import marimo

__generated_with = "0.23.10"
app = marimo.App(width="medium")


@app.cell
def _():
    import sys
    from pathlib import Path

    import marimo as mo

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import serve_manager

    return Path, mo, serve_manager


@app.cell
def _(mo):
    mo.md("""
    # 🛡️ Hexgate — define a live agent

    A **throwaway sandbox** — everything vanishes when it scales down.

    1. Paste your **OpenAI API key** below and **submit** (press Enter or click
       the button) — that starts the agent.
    2. Edit the **tools** and **agent** cells (real Python) — changes live-reload.
    3. Open the playground to chat and watch **policy decisions**
       (allow / deny / approval) stream live.
    """)
    return


@app.cell
def _(mo):
    import os

    # 🔑 Paste your key, then press Enter (or click "Apply & start") to submit.
    # Wrapping the text box in .form() is what gives an explicit submit — a plain
    # mo.ui.text has none, which is why "Enter" felt like it did nothing.
    #
    # The value lives ONLY in the running kernel — marimo saves code, not widget
    # state — so it never touches this file and is gone after a restart. Prefills
    # from the OPENAI_API_KEY env var if you set one before launching.
    key_form = mo.ui.text(
        kind="password",
        label="OpenAI API key",
        placeholder="sk-...",
        value=os.environ.get("OPENAI_API_KEY", ""),
        full_width=True,
    ).form(submit_button_label="▶ Apply & start agent")
    key_form
    return (key_form,)


@app.cell
def _(mo):
    mo.md("""
    ### 1 · Tools — define as many as you like (`@tool`)
    """)
    return


@app.cell
def _():
    # Tools are plain LangChain tools. Edit / add freely, then re-run this cell.
    # (Built-ins also exist: `from hexgate.tools import web_search, fetch, ...`.)
    from langchain_core.tools import tool

    @tool
    def get_order_status(order_id: str) -> str:
        """Look up the delivery status of an order by its id."""
        return f"Order {order_id}: shipped, arriving Tuesday."

    @tool
    def refund_order(order_id: str, amount: float) -> str:
        """Issue a refund of `amount` USD for `order_id`. A side-effecting tool."""
        return f"Refunded ${amount:.2f} for order {order_id}."


    @tool
    def whoami() -> str:
        """Return user name"""
        return "A hexgates' hexamind user"

    TOOLS = [get_order_status, refund_order, whoami]
    return (TOOLS,)


@app.cell
def _(mo):
    mo.md("""
    ### 2 · Agent — `create_agent(...)` returns a live object
    """)
    return


@app.cell
def _(TOOLS):
    # A real agent object in the kernel — edit model / prompt / tools and re-run.
    from hexgate import create_agent

    agent, _handler = create_agent(
        model="gpt-4o-mini",
        tools=TOOLS,
        system_prompt=(
            "You are a customer support agent. Help with orders and refunds. "
            "Confirm details before issuing a refund."
        ),
        name="demo_agent",
    )
    return (agent,)


@app.cell
def _(agent, key_form, mo, serve_manager):
    # Runs when you submit the key form (Enter or the button). `key_form.value`
    # is None until submitted. Uses the live `agent` object from the cell above —
    # edit/re-run that cell and it live-reloads with the same key.
    if key_form.value:
        serve_manager.apply(agent, key_form.value)
        out = mo.md("✅ **Running.** Open the playground below to chat with your agent.")
    else:
        out = mo.md(
            f"Agent serve status: **{serve_manager.status()}** — "
            "paste your key above and submit to start."
        )
    out
    return


@app.cell
def _(Path, mo):
    # Dashboard URL written by boot.py (the public tunnel, or localhost in dev).
    # /v1/demo-login signs the visitor in, then redirects to /playground.
    # from pathlib import Path

    dash_url = Path("/tmp/hexgate_dash_url").read_text().strip().rstrip("/")
    login_url = f"{dash_url}/v1/demo-login"

    mo.md(
        f"""
        ## ▶ Open the live playground

        ### [Chat with your agent → ]({login_url})

        Opens the dashboard in a new tab, signed in. After **Apply & reload**,
        send a message there and watch reasoning, tool calls, and policy
        decisions stream live. Edit the policy in the **Policies** tab — the next
        message picks it up.
        """
    )
    return


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
