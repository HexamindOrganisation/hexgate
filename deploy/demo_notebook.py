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

    1. Type your **OpenAI API key** in the box below, then click
       **▶ Apply & start agent**.
    2. Edit the **tools** and **agent** cells (real Python) — click Apply again
       to live-reload.
    3. Open the playground to chat and watch **policy decisions**
       (allow / deny / approval) stream live.
    """)
    return


@app.cell
def _(mo):
    # 🔑 Type your key in the box, then click the button. The box value is live
    # as you type (no Enter needed); the button is what applies it. The value
    # lives only in the running kernel — never written to this file.
    api_key = mo.ui.text(kind="password", placeholder="sk-...", full_width=True)
    start = mo.ui.run_button(label="▶ Apply & start agent")
    mo.vstack([mo.md("**OpenAI API key**"), api_key, start])
    return api_key, start


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
def _(agent, api_key, mo, serve_manager, start):
    # Fires on button click. Reads the live `api_key.value`, sets it in the
    # process env (so the agent's OpenAI client picks it up) AND hands it to
    # serve_manager. Uses the live `agent` object — edit/re-run that cell and
    # click again to live-reload.
    import os

    if start.value:
        if not api_key.value:
            out = mo.md("⚠️ **Type your OpenAI key in the box above**, then click Apply.")
        else:
            os.environ["OPENAI_API_KEY"] = api_key.value
            serve_manager.apply(agent, api_key.value)
            out = mo.md(
                f"✅ **Running** with key `…{api_key.value[-4:]}`. "
                "Open the playground below to chat."
            )
    else:
        out = mo.md(
            f"Agent serve status: **{serve_manager.status()}** — "
            "type your key above and click **Apply & start**."
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
