"""Hexgate guards platform demo (marimo) — govern a plugin from the dashboard.

The landing UI for a disposable demo container. Read top to bottom:
  1. see the two tools + the two guards attached to the agent,
  2. enter your OpenAI key and start it,
  3. open the dashboard and chat — `secret_redactor` replaces a secret in `send_message`'s
     args, and `secret_scrubber` replaces a secret in `search_text`'s result,
  4. flip either guard's `enabled: false` in the Policies tab, Save, and resend — the very
     next turn that plugin is off. Live.

The agent runs in-kernel (serve_manager runs the `hexgate serve` loop bound to the
live `agent` object) and streams to the dashboard. BYOK: your key lives only in this
throwaway container and is never written to disk.

Run by boot.py via `marimo edit` (see `make demo-guards`).
"""

import marimo

__generated_with = "0.23.10"
app = marimo.App(width="medium")


@app.cell
def _():
    import sys
    from pathlib import Path

    import marimo as mo

    # serve_manager lives in deploy/; this notebook is deploy/guards-demo/.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import serve_manager

    return Path, mo, serve_manager


@app.cell
def _(mo):
    mo.md("""
    # 🔌 Hexgate — govern a plugin from the platform

    A **throwaway sandbox** — everything vanishes when it scales down.

    This agent ships with **two official plugins**, each scoped to one tool:

    - **`secret_redactor`** on **`send_message`** — strips a credential **out of the args**
      before the call runs, so the message still sends with the secret replaced by a
      `[REDACTED:<kind>]` marker (the *outbound* case).
    - **`secret_scrubber`** on **`search_text`** — strips a credential that comes back **in the
      result** of a search, so the snippet returns with the secret replaced by a
      `[REDACTED:<kind>]` marker (the *inbound* case).

    You **turn each plugin on and off from the dashboard** and watch the change take effect
    on the very next message — no restart.

    1. **See the two tools + two guards** the agent is built with.
    2. **Enter your OpenAI key and start it.**
    3. **Open the playground** and try both: send a message with a key (watch it get
       **redacted**), and run a search (watch `secret_scrubber` **redact** the leaked key
       out of the result).
    4. In the **Policies** tab, flip either guard's `enabled: false`, **Save**, and resend —
       that plugin is now **off**.
    """)
    return


@app.cell
def _(mo):
    # Two guards, one per tool: a BEFORE guard that rewrites outbound args, and an AFTER
    # guard that rewrites the inbound result. The policy's guards: block decides whether
    # each fires — read fresh on every call.
    _diagram = mo.mermaid(
        """
        flowchart LR
            Agent["🤖 agent"]
            Agent -->|"send_message(args)"| R["🔌 secret_redactor<br/>(before)"]
            R -->|"strip secret → [REDACTED]"| SM["📤 send_message runs"]
            Agent -->|"search_text(query)"| ST["🔎 search_text runs"]
            ST -->|"result"| W["🔌 secret_scrubber<br/>(after)"]
            W -->|"strip secret → [REDACTED]"| Out["📥 result returned"]
        """
    )
    mo.vstack(
        [
            mo.md("## How the two governed plugins work"),
            _diagram,
            mo.md(
                "Each plugin is **code** scoped to one tool "
                "(`before_tool(['send_message'])` / `after_tool(['search_text'])`). Whether "
                "each **runs** is **policy**: the `guards:` block in the dashboard. The "
                "stance is read on **every** call, so flipping one in the Policies tab takes "
                "effect on the next message — no rebuild, no restart."
            ),
        ]
    )
    return


@app.cell
def _(mo):
    mo.md("""
    ## 1 · The tools + the guards
    """)
    return


@app.cell
def _():
    # Two plain LangChain tools, one per guard:
    #   send_message — OUTBOUND: a pasted credential rides into its `message` arg, so it
    #                  is where `secret_redactor` strips one before the call runs.
    #   search_text  — INBOUND: its result is a knowledge-base snippet that may carry a
    #                  credential, so it is where `secret_scrubber` strips one on the way back.
    from langchain_core.tools import tool

    @tool
    def send_message(recipient: str, message: str) -> str:
        """Send `message` to `recipient` (e.g. a Slack handle or email)."""
        return f"Sent to {recipient}: {message!r}"

    @tool
    def search_text(query: str) -> str:
        """Search the internal knowledge base for `query`; return the top snippet."""
        # Canned hit: an old runbook note that leaked a credential — exactly the shape
        # secret_scrubber exists to strip when it comes back from a read/search tool.
        return (
            f"Top match for {query!r} — ops runbook (2023): "
            "'prod deploy creds: AKIAUJZDE8GXD6NCF10E — rotate before Q2'."
        )

    TOOLS = [send_message, search_text]
    return (TOOLS,)


@app.cell
def _(mo):
    mo.md("""
    ## 2 · The guarded agent
    """)
    return


@app.cell
def _(TOOLS):
    # A factory — the actual agent is built on Start (step 3), AFTER your OpenAI key is
    # in env (create_agent(model=str) builds ChatOpenAI, which validates the key at
    # construction). On Start, serve registers this agent's guard-declaring manifest
    # into the seeded project, where the top-level `guards:` policy governs every agent
    # — so the dashboard's declared-guards view and the live toggle both light up.
    from hexgate import create_agent
    from hexgate.guards import after_tool, before_tool
    from hexgate.plugins import secret_redactor, secret_scrubber

    # Scope each official plugin to the one tool it demonstrates. Wrapping `.fn` keeps the
    # guard's label (`secret_redactor` / `secret_scrubber`), so the policy still governs each
    # by name — and the manifest now records WHICH tool each is scoped to.
    redact_send = before_tool(tool_names=["send_message"])(secret_redactor.fn)
    scrub_search = after_tool(tool_names=["search_text"])(secret_scrubber.fn)

    def build_agent():
        return create_agent(
            model="gpt-4o-mini",
            tools=TOOLS,
            # secret_redactor (before send_message) strips a credential out of the args —
            # the call still runs, with the secret replaced by a [REDACTED:kind] marker.
            # secret_scrubber (after search_text) strips a credential that comes back in the
            # result — the call still returns, with the secret replaced the same way. Both
            # are declared on the manifest, so the policy governs them by name.
            guards=[redact_send, scrub_search],
            system_prompt=(
                "You are a helpful assistant. Use send_message to deliver messages and "
                "search_text to look things up, exactly as the user asks."
            ),
            name="guarded_demo",
        )

    return (build_agent,)


@app.cell
def _(mo):
    mo.md("""
    ## 3 · Add your OpenAI key & start
    """)
    return


@app.cell
def _(mo):
    # Value is live as you type (no Enter needed); the button starts the agent. The key
    # lives only in the running kernel — never written to this file.
    api_key = mo.ui.text(kind="password", placeholder="sk-...", full_width=True)
    start = mo.ui.run_button(label="▶ Start agent")
    mo.vstack([mo.md("**OpenAI API key**"), api_key, start])
    return api_key, start


@app.cell
def _(api_key, build_agent, mo, serve_manager, start):
    # Fires on button click: set the key, build the guarded agent (post-key), (re)start
    # the in-kernel serve loop bound to it (auto-registers its guard-declaring manifest),
    # then report the REAL status.
    import os
    import time

    if start.value:
        if not api_key.value:
            out = mo.md("⚠️ **Enter your OpenAI key above**, then click Start.")
        else:
            os.environ["OPENAI_API_KEY"] = api_key.value  # BYOK
            agent, _handler = build_agent()
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
                out = mo.md(
                    f"⏳ **{st}** — give it a few seconds and click Start again."
                )
    else:
        out = mo.md(
            f"Agent status: **{serve_manager.status()}** — "
            "enter your key above and click **Start agent**."
        )
    out
    return


@app.cell
def _(mo):
    mo.md("""
    ## The policy that governs the plugins

    The project's **`policy.yaml`** entry file carries a **`guards:`** block. A guard the
    policy doesn't mention runs as coded (enabled); a guard set `enabled: false` is
    **skipped** — read fresh on every call, so a change is live on the next message.

    ```yaml
    version: 1

    guards:
      secret_redactor: { enabled: true }   # scoped to send_message
      secret_scrubber: { enabled: true }   # scoped to search_text

    tools:
      send_message: { mode: allow }
      search_text:  { mode: allow }
    ```

    Both are **on** by default. The demo below flips each **off** and back — live, from
    the dashboard.
    """)
    return


@app.cell
def _(mo):
    mo.md("""
    ## 4 · Open the playground & flip the plugins
    """)
    return


@app.cell
def _(Path, mo):
    # Dashboard URL written by boot.py (the public tunnel, or localhost in dev).
    # /v1/demo-login signs the visitor in, then redirects to /playground.
    dash_url = Path("/tmp/hexgate_dash_url").read_text().strip().rstrip("/")
    login_url = f"{dash_url}/v1/demo-login"

    mo.md(
        f"""
        ### [▶ Open the dashboard →]({login_url})

        Opens signed in, in a new tab. Copy the key **exactly** — the plugins match real
        credential **formats** (`AKIA` + 16 chars, OpenAI `sk-proj-…`, …), not any random
        string; a made-up string like `sk_kwoiwef_123` is passed through untouched, which is
        the detector avoiding false positives, not a broken plugin.

        **A · `secret_redactor` on `send_message` (outbound).** Send:

        > *Send a message to #ops with this AWS key: `AKIAIOSFODNN7EXAMPLE`*

        `send_message` still runs, but its args show the key **replaced** with
        `[REDACTED:aws_access_key]` — the raw secret never reaches the tool. Flip
        `secret_redactor: enabled: false` in **Policies**, Save, resend → the **raw** key
        rides straight into `send_message`. Flip back → it redacts again.

        **B · `secret_scrubber` on `search_text` (inbound).** Send:

        > *Search the knowledge base for the deploy runbook*

        `search_text` returns a snippet that contains a leaked `AKIA…` key. `secret_scrubber`
        **strips** it: the snippet comes back with the key **replaced** by
        `[REDACTED:aws_access_key]`, so the raw secret never reaches the model or your screen
        — the search still succeeds. Flip `secret_scrubber: enabled: false` in **Policies**,
        Save, resend → the **raw** `AKIA…` key comes straight back in the result. Flip back →
        it scrubs again.
        """
    )
    return


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
