"""Hexgate live-demo notebook (marimo) — define tools + agent, then run it.

The landing UI for a disposable demo container. Read top to bottom:
  1. define your tools,
  2. define your agent,
  3. enter your OpenAI key and start it,
  4. open the dashboard playground to chat.

The agent runs in-kernel (serve_manager runs the `hexgate serve` loop bound to
the live `agent` object) and streams to the dashboard. BYOK: your key lives only
in this throwaway container and is never written to disk.

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
    mo.md(
        """
        # 🛡️ Hexgate — define & run a live agent

        A **throwaway sandbox** — everything vanishes when it scales down.

        1. **Define your tools** (real Python).
        2. **Define your agent.**
        3. **Enter your OpenAI key and start it.**
        4. **Open the playground** to chat and watch policy decisions stream.

        Edit the tools/agent cells anytime, then click **Start** again to reload.
        """
    )
    return


@app.cell
def _(mo):
    mo.md("## 1 · Define your tools")
    return


@app.cell
def _():
    # Plain LangChain tools — edit / add freely, then re-run this cell. (Built-ins
    # exist too: `from hexgate.tools import web_search, fetch, read_file, ...` —
    # note web_search needs LINKUP_API_KEY, fetch needs TAVILY_API_KEY.)
    from langchain_core.tools import tool

    @tool
    def get_order_status(order_id: str) -> str:
        """Look up the delivery status of an order by its id."""
        return f"Order {order_id}: shipped, arriving Tuesday."

    @tool
    def refund_order(order_id: str, amount: float) -> str:
        """Issue a refund of `amount` USD for `order_id`. A side-effecting tool."""
        return f"Refunded ${amount:.2f} for order {order_id}."

    TOOLS = [get_order_status, refund_order]
    return (TOOLS,)


@app.cell
def _(mo):
    mo.md("## 2 · Define your agent")
    return


@app.cell
def _(TOOLS):
    # A real agent object in the kernel — edit model / prompt / tools and re-run,
    # then click Start (step 3) to reload it into the playground.
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
def _(mo):
    mo.md("## 3 · Add your OpenAI key & start")
    return


@app.cell
def _(mo):
    # Value is live as you type (no Enter needed); the button starts the agent.
    # The key lives only in the running kernel — never written to this file.
    api_key = mo.ui.text(kind="password", placeholder="sk-...", full_width=True)
    start = mo.ui.run_button(label="▶ Start agent")
    mo.vstack([mo.md("**OpenAI API key**"), api_key, start])
    return api_key, start


@app.cell
def _(agent, api_key, mo, serve_manager, start):
    # Fires on button click: set the key in the env (the agent's OpenAI client
    # reads it at call time), (re)start the in-kernel serve loop bound to the
    # live `agent`, then report the REAL status so you see it actually connected.
    import os
    import time

    if start.value:
        if not api_key.value:
            out = mo.md("⚠️ **Enter your OpenAI key above**, then click Start.")
        else:
            os.environ["OPENAI_API_KEY"] = api_key.value  # BYOK
            serve_manager.apply(agent)
            time.sleep(3)  # let it build the runtime, auto-register + dial /v1/serve
            st = serve_manager.status()
            if st == "running":
                out = mo.md(
                    f"✅ **Agent running** (key `…{api_key.value[-4:]}`). "
                    "Open the playground below."
                )
            elif st.startswith("error"):
                out = mo.md(f"❌ **Failed to start:** `{st}`")
            else:
                out = mo.md(f"⏳ **{st}** — give it a few seconds and click Start again.")
    else:
        out = mo.md(
            f"Agent status: **{serve_manager.status()}** — "
            "enter your key above and click **Start agent**."
        )
    out
    return


@app.cell
def _(mo):
    mo.md("## 4 · Open the playground")
    return


@app.cell
def _(Path, mo):
    # Dashboard URL written by boot.py (the public tunnel, or localhost in dev).
    # /v1/demo-login signs the visitor in, then redirects to /playground.
    dash_url = Path("/tmp/hexgate_dash_url").read_text().strip().rstrip("/")
    login_url = f"{dash_url}/v1/demo-login"

    mo.md(
        f"""
        ### [▶ Chat with your agent →]({login_url})

        Opens the dashboard in a new tab, signed in. Send a message and watch the
        reasoning, tool calls, and **policy decisions** (allow / deny / approval)
        stream live. Edit the policy in the **Policies** tab — the next message
        picks it up.
        """
    )
    return


if __name__ == "__main__":
    app.run()
