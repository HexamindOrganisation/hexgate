# fortify

`fortify` is a lightweight LangChain-based agent runtime built around:

- `langchain`
- `gpt-5.4`
- `Linkup` web search
- Tavily-based page fetch
- `Langfuse` tracing

This package is intentionally small. The first milestone is a single assistant with:

- `web_search`
- `fetch`

## 🛠️ Prerequisites

The SDK itself only needs Python — but a few of the bundled tools shell out to native binaries that you'll want installed on the host before running an agent that uses them.

| Required when you use… | Install |
|---|---|
| **`grep`, `glob`, `bash`, `read_file`, `edit_file`, `write_file`** — anything filesystem-shaped | [`ripgrep`](https://github.com/BurntSushi/ripgrep) — `brew install ripgrep` (macOS), `apt install ripgrep` (Debian/Ubuntu), `winget install BurntSushi.ripgrep.MSVC` (Windows) |
| **The dashboard** under `platform/dashboard/` | Node 18+ and `pnpm` — `corepack enable` or `npm i -g pnpm` |
| **The control plane** under `platform/api/` | [`uv`](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

`web_search` and `fetch` have no system dependencies — pure Python. If you're only using those, ignore the table above.

The runtime preflights `ripgrep` at agent build time and refuses to start when it's missing — fail-fast is friendlier than silently falling back to a 100× slower path.

## ⚡ Quick Start — Local CLI

If you just want to install `fortify` and try the terminal chat:

1. Install the package in editable mode.
2. Copy the sample environment file.
3. Fill in the required API keys.
4. Run the chat CLI against the included local example agent.

```bash
python -m pip install -e .
cp .env.sample .env
fortify --agent example_agent
```

Required keys for the example CLI flow:

- `OPENAI_API_KEY`
- `LINKUP_API_KEY`
- `TAVILY_API_KEY`

Useful next commands:

```bash
fortify --list-agents
fortify --agent researcher
fortify --use examples/file_agents.py --agent workspace_explorer
fortify --use examples/research_agents.py --agent update_researcher
```

The included local agent lives in `examples/example_agent/`, and the CLI can also load:

- builtin packaged agents like `researcher`
- code-defined agents registered from `examples/file_agents.py`
- code-defined research agents registered from `examples/research_agents.py`

## 🚀 Quick Start — Platform

To run the full Fortify control plane locally (backend + dashboard + your local agent serving over WebSocket), you need three terminals:

```bash
# Terminal 1 — backend (FastAPI + SQLite)
cd platform/api
uv run uvicorn main:app --reload --port 8000

# Terminal 2 — dashboard (Vite + React)
cd platform/dashboard
pnpm install        # first run only
pnpm dev

# Terminal 3 — mint a token, then serve your local agent
# 1. Open http://localhost:5173/tokens
# 2. Click "Mint new token", copy the value
# 3. Add to asianf/.env:
#        FORTIFY_KEY=fty_test_support-bot_...
# 4. Start serve mode:
uv run fortify --serve
```

Then open http://localhost:5173/playground — type a message, watch the live stream of tool calls and policy decisions from your local agent.

The dashboard's `/agents` page lets you edit each agent's YAML and policy. `fortify --serve` re-fetches at every turn boundary, so your edits take effect on the next chat message without a restart.

## ✨ Core Primitives

The two main primitives are:

- `create_agent(...)`
- `@agent_tool(...)`

Use them when you want to define everything directly in Python.

```python
from fortify import agent_tool, create_agent


@agent_tool(name="my_lookup")
async def my_lookup(query: str) -> dict:
    """Look up something useful."""
    return {"query": query, "results": []}

agent, handler = create_agent(
    model="openai:gpt-5.4",
    tools=[my_lookup],
    system_prompt="You are a helpful research assistant.",
)
```

## 📦 What You Can Import

The current curated surface includes:

- `create_agent`
- `enforce_policy`
- `with_approval_handler`
- `with_before_action`
- `invoke_agent`
- `stream_agent`
- `stream_agent_raw`
- `load_builtin_agent`
- `list_builtin_agents`
- `load_fortify_agent`
- `User` — async context manager for per-request user attenuation (see [User Scope](#-user-scope))
- `agent_tool`
- `web_search`
- `fetch`

Example:

```python
from fortify import (
    create_agent,
    edit_file,
    enforce_policy,
    glob,
    grep,
    read_file,
    write_file,
    with_approval_handler,
    with_before_action,
    agent_tool,
    load_agent,
    load_builtin_agent,
    load_fortify_agent,
    register_agent,
    fetch,
    web_search,
    User,
)
```

## 🤝 Framework Agent Wrapping

In addition to its native `create_agent(...)` runtime, `fortify` ships adapters that wrap agents built with **OpenAI Agents SDK**, **LangChain / LangGraph**, **Google ADK**, or **Pydantic AI** to add two things without touching the agent's logic:

1. **Tool-call policy enforcement.** Each tool the agent can invoke is gated by an `AgentPolicy` that decides allow/deny per call. Denied calls return a denial string (or framework-native exception) to the model rather than aborting the run, so the agent can recover.
2. **User-aware observability.** Every run is traced through Langfuse with the caller's `UserContext` (user id, session id, role) propagated onto the spans.

The four integrations differ in shape because the underlying SDKs do:

| | OpenAI Agents SDK | LangChain / LangGraph | Google ADK | Pydantic AI |
| --- | --- | --- | --- | --- |
| Entry point | `FortifyRunner` (replaces `Runner`) | `wrap_langchain_agent` (returns a proxy) | `FortifyRunner` (replaces `Runner`) | `wrap_pydantic_agent` (returns a proxy) |
| Tool wrapping | Copies each `FunctionTool`, replaces `on_invoke_tool` with a guarded version | Mutates each `BaseTool` in place, replaces `func`/`coroutine` with contextvar-driven gates, sets `handle_tool_error=True` | Copies each `BaseTool` (normalizing bare callables to `FunctionTool`), replaces `run_async` with a guarded version | Copies each `Tool` and overrides `function_schema.call` with a contextvar-driven gate |
| Denial behavior | Guard returns the denial text as tool output | Guard raises `ToolDeniedError` (a `ToolException`); LangChain converts it to a `ToolMessage` | Guard returns the denial text as tool output | Guard raises `ToolDeniedError` (a `ModelRetry`); pydantic_ai surfaces it back to the model as a tool-result message |
| Tracing | `OpenAIAgentsInstrumentor` + `propagate_attributes` | Langfuse `CallbackHandler` injected into each call's `RunnableConfig` + `propagate_attributes` | `GoogleADKInstrumentor` + `propagate_attributes` | `Agent.instrument_all()` + `propagate_attributes` |

In all cases, the original agent object is left intact (or, for LangChain tools, mutated by design so the same `tools` list flows through `create_react_agent`); the wrapper holds the policy and the user context.

All adapters resolve the API key the same way: from the explicit `api_key=` argument, falling back to the `FORTIFY_KEY` environment variable.

### OpenAI Agents SDK — `FortifyRunner`

`FortifyRunner` is a drop-in replacement for `agents.Runner`. It wraps the agent's tools with the policy resolved for the user, then dispatches to `Runner.run` / `run_sync` / `run_streamed`.

```python
import asyncio
from agents import Agent, function_tool
from dotenv import load_dotenv

from fortify.runtime import UserContext
from fortify.adapters.openai import FortifyRunner


@function_tool
def get_weather(city: str) -> str:
    return f"{city}: sunny, 23°C"


async def main():
    load_dotenv()

    agent = Agent(
        name="Weather Agent",
        instructions="Use get_weather when asked about weather.",
        tools=[get_weather],
        model="gpt-4o-mini",
    )

    runner = FortifyRunner()  # picks up FORTIFY_KEY from env
    result = await runner.run(
        agent,
        "What's the weather in Cherbourg?",
        user_context=UserContext(
            user_id="user_1", session_id="session_1", user_role="member",
        ),
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
```

What happens under the hood:

- `FortifyRunner.run` calls `wrap_openai_agent`, which builds an `AgentPolicy` for `(user_context, agent.name, tool_names)` and returns a `dataclasses.replace`'d copy of the agent with policy-gated tool copies — your original `agent` is untouched.
- When the model calls a tool, the guard checks the policy. On deny, it returns `"Tool '<name>' is denied by the agent policy. The tool was not executed."` so the model sees a tool result and can adapt.
- The run executes inside `propagate_attributes(user_id=..., session_id=..., metadata={"user_role": ...})`, so Langfuse spans carry the caller identity.

`run_sync` and `run_streamed` work the same way.

### LangChain / LangGraph — `wrap_langchain_agent`

`wrap_langchain_agent` mutates the tools you pass in (so the same instances inside the compiled graph become policy-gated) and returns a `FortifyLangchainAgent` proxy that injects a Langfuse callback into every `invoke` / `ainvoke` / `stream` / `astream` / `astream_events` call. The `user_context` is supplied **per call**, not at wrap time, so a single wrapped agent can serve many users concurrently — each call resolves its own `AgentPolicy` and identity propagation.

```python
import asyncio
from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from fortify.runtime import UserContext
from fortify.adapters.langchain import wrap_langchain_agent


@tool
def get_weather(city: str) -> str:
    """Return a weather report for a city."""
    return f"The weather in {city} is 21°C and sunny."


@tool
def delete_user(user_id: str) -> str:
    """Delete a user account. Destructive."""
    return f"User {user_id} deleted."


TOOLS = [get_weather, delete_user]


async def main():
    load_dotenv()

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    graph = create_react_agent(llm, TOOLS)

    agent = wrap_langchain_agent(
        agent=graph,
        tools=TOOLS,          # same list passed to create_react_agent — wrapped in place
        api_key="sk-...",     # or rely on FORTIFY_KEY
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "What is the weather in Tokyo?"}]},
        user_context=UserContext(
            user_id="langchain_user_1",
            user_role="member",
            session_id="session_abc",
        ),
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
```

What happens under the hood:

- `wrap_langchain_agent` calls `wrap_tools(tools)`, which replaces each tool's `func` and `coroutine` with guards that read the active policy from a `ContextVar` at call time. `handle_tool_error` is forced to `True`. Wrapping is idempotent — the same tool instance can be passed through `wrap_langchain_agent` multiple times without double-installing.
- Each invocation method on `FortifyLangchainAgent` takes `user_context=` and, before delegating to the underlying `CompiledStateGraph`, resolves an `AgentPolicy` for `(user_context, agent.name, tool_names)` and binds it to the contextvar via `active_policy(...)`. The contextvar is per-task, so concurrent `ainvoke` calls for different users do not see each other's policies.
- A denied call raises `ToolDeniedError` (a `ToolException`); LangChain catches it because of `handle_tool_error=True` and emits a `ToolMessage` with the denial text — the tool body never runs. If a guarded tool is invoked outside any `active_policy(...)` scope (i.e. without going through the wrapped agent), it denies by default.
- The wrapper also enters `propagate_attributes(user_id=..., session_id=..., metadata={"user_role": ...})` for the duration of the call and merges a Langfuse `CallbackHandler` into the `RunnableConfig.callbacks`. Anything not explicitly proxied falls through via `__getattr__`.

### Google ADK — `FortifyRunner`

The Google ADK wrapper exposes its own `FortifyRunner`. Unlike the OpenAI variant, it is constructed up front with the agent, app name, and session service (mirroring the ADK `Runner` constructor); `run` / `run_async` then yield ADK events.

```python
import asyncio
from datetime import datetime

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.sessions import InMemorySessionService
from google.genai import types

from fortify.runtime import UserContext
from fortify.adapters.google import FortifyRunner


def get_weather(city: str) -> str:
    """Get the current weather for a given city."""
    return f"{city}: sunny, 23°C, humidity 50%, wind 10 m/s"


def get_current_time() -> str:
    """Return the current local time as an ISO-8601 string."""
    return datetime.now().isoformat()


async def main():
    load_dotenv()

    agent = Agent(
        name="google_runner_example_agent",
        model=LiteLlm(model="openai/gpt-4o"),
        instruction="Use get_current_time and get_weather when asked.",
        tools=[get_current_time, get_weather],
    )

    user_context = UserContext(
        user_id="google_user_1",
        session_id="google_session_1",
        user_role="user",
    )

    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="google_runner_example",
        user_id=user_context.user_id,
        session_id=user_context.session_id,
    )

    runner = FortifyRunner(
        agent=agent,
        app_name="google_runner_example",
        session_service=session_service,
    )  # picks up FORTIFY_KEY from env

    user_msg = types.Content(
        role="user", parts=[types.Part(text="What is the weather in New Delhi?")]
    )

    async for event in runner.run_async(
        new_message=user_msg, user_context=user_context
    ):
        if event.is_final_response():
            print(event.content.parts[0].text)


if __name__ == "__main__":
    asyncio.run(main())
```

What happens under the hood:

- On each `run` / `run_async`, `FortifyRunner` calls `wrap_google_agent`, which builds an `AgentPolicy` for `(user_context, agent.name, tool_names)` and returns `agent.model_copy(update={"tools": guarded_tools})` — your original `agent` is untouched.
- Each tool is normalized first: bare callables in `agent.tools` are wrapped into `FunctionTool` (matching what ADK does internally) so the guard has a stable `BaseTool` surface. Each tool is then `copy.copy`'d and its `run_async` replaced with a guarded version.
- On deny, the guard returns `"Tool '<name>' is denied by the agent policy. The tool was not executed."` so the ADK runtime forwards it to the model as the tool output instead of aborting the run.
- Observability is set up lazily on each call: `GoogleADKInstrumentor().instrument()` plus `nest_asyncio.apply()` (ADK's runner spins its own loop), and the run executes inside `propagate_attributes(user_id=..., session_id=..., metadata={"user_role": ...}, tags=["google.runner.run.<agent_name>"])` so Langfuse spans carry the caller identity.

### Pydantic AI — `wrap_pydantic_agent`

`wrap_pydantic_agent` returns a `FortifyPydanticAgent` proxy backed by a clone of the original agent whose tools are gated by the policy. Tools registered via the `Agent(...)` constructor or via `@agent.tool` / `@agent.tool_plain` are all picked up. The `user_context` is supplied **per call**, not at wrap time, so a single wrapped agent can serve many users concurrently — each call resolves its own `AgentPolicy` and identity propagation.

```python
import asyncio
from dotenv import load_dotenv
from pydantic_ai import Agent

from fortify.runtime import UserContext
from fortify.adapters.pydantic_ai import wrap_pydantic_agent


async def main():
    load_dotenv()

    agent = Agent("openai:gpt-4o-mini")

    @agent.tool_plain
    def get_weather(city: str) -> str:
        """Return a weather report for a city."""
        return f"The weather in {city} is 21°C and sunny."

    @agent.tool_plain
    def delete_user(user_id: str) -> str:
        """Delete a user account. Destructive."""
        return f"User {user_id} deleted."

    agent = wrap_pydantic_agent(
        agent=agent,
        api_key="sk-...",  # or rely on FORTIFY_KEY
    )

    result = await agent.run(
        "What is the weather in Tokyo?",
        user_context=UserContext(
            user_id="pydantic_ai_user_1",
            user_role="member",
            session_id="pydantic_ai_session_1",
        ),
    )
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
```

What happens under the hood:

- `wrap_pydantic_agent` reads tools off the agent's internal `_function_toolset`, copies each tool with a guarded `function_schema.call` that reads the active policy from a `ContextVar` at call time, and returns a shallow-copied agent whose toolset holds those gated copies — your original `agent` is untouched, so it can be reused or wrapped again independently.
- Each invocation method on `FortifyPydanticAgent` (`run` / `run_sync` / `run_stream` / `iter`) takes `user_context=` and, before delegating to the underlying `Agent`, resolves an `AgentPolicy` for `(user_context, agent.name, tool_names)` and binds it to the contextvar via `active_policy(...)`. The contextvar is per-task, so concurrent `run` calls for different users do not see each other's policies.
- A denied call raises `ToolDeniedError` (a `ModelRetry`); pydantic_ai surfaces it back to the model as a tool-result message instead of aborting the run. If a guarded tool is invoked outside any `active_policy(...)` scope (i.e. without going through the wrapped agent), it denies by default.
- Identity propagation uses `propagate_attributes(...)` so Langfuse spans carry the caller identity. Global tracing is enabled via `Agent.instrument_all()` on construction.

### Runnable examples

Working scripts in `examples/`:

- `examples/openai_demo.py` — `FortifyRunner` (OpenAI Agents SDK) end-to-end.
- `examples/langchain_demo.py` — `wrap_langchain_agent` (LangChain) end-to-end with `create_react_agent`.
- `examples/google_demo.py` — `FortifyRunner` (Google ADK) end-to-end with `InMemorySessionService`.
- `examples/pydantic_ai_demo.py` — `wrap_pydantic_agent` (Pydantic AI) end-to-end.

> **Note on naming.** These demo files end in `_demo.py` so their filenames don't shadow the installed packages they import (`agents`, `google`, `langchain`, `openai`, `pydantic_ai`). Without the suffix, running any script inside `examples/` would put the directory on `sys.path[0]` and Python would import the demo files instead of the real packages.

## 🧠 Define Agents In Code

You can define agents directly in Python with `create_agent(...)`.

If you want the CLI and shared loader to resolve that agent by name, register it first and then load it through `load_agent(...)`.

A small end-to-end example registry lives in:

- `examples/file_agents.py`
- `examples/research_agents.py`

It demonstrates:

- building one agent with `create_agent(...)` only
- building another with `create_agent(...)` plus `enforce_policy(...)`
- building a research agent with approval-gated file writes
- registering it with `register_agent(...)`
- loading it through the shared `load_agent(...)` path

For the CLI, you can import that script and then pick one of its registered agents:

```bash
fortify --use examples/file_agents.py --agent workspace_explorer
fortify --use examples/file_agents.py --agent repo_editor
fortify --use examples/research_agents.py --agent update_researcher
```

## 🗂️ Builtin And Local Agents

The package now ships with a small `fortify.builtin_agents` directory for official starter agents.

Current builtin agents:

- `researcher`

Example:

```python
from fortify import load_builtin_agent

agent, handler = load_builtin_agent("researcher")
```

The CLI also discovers local agents from:

- `./<agent_dir>/agent.yaml`
- `./agents/<agent_dir>/agent.yaml`
- `./examples/<agent_dir>/agent.yaml`

This repo ships a demo agent at `examples/example_agent/`, so from the project root you can simply run:

```bash
fortify --agent example_agent
```

## 🔐 Policy Shape

Each tool gets a mode and an optional list of constraints:

```yaml
version: 1

default_policy:
  mode: deny

tools:
  web_search:
    mode: allow
  fetch:
    mode: allow
  refund_order:
    mode: allow
    constraints:
      - args.amount <= 500
      - args.currency == "USD"
```

Supported modes:

- `allow`
- `deny`
- `approval_required`

Constraint operators: `==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not in`. Strings on the right use JSON double quotes. Every constraint must pass for the call to authorize (implicit AND). See [User Scope + Roles](#-user-scope--roles) for the role-aware policy bundle shape that picks a per-role policy at call time.

## 🛡️ Gate 1: Local Policy Enforcement

Gate 1 is the current built-in security layer.

Use it when:

- developers should be able to build and test agents freely
- a host platform or admin layer wants to constrain tool access later
- you want deny-by-default behavior before a tool actually runs

`create_agent(...)` stays close to LangChain. Policy enforcement is applied after agent creation:

```python
from fortify import AgentPolicy, create_agent, enforce_policy, fetch, web_search

policy = AgentPolicy.model_validate(
    {
        "version": 1,
        "default_policy": {"mode": "deny"},
        "tools": {
            "web_search": {"mode": "allow"},
            "fetch": {"mode": "allow"},
        },
    }
)

agent, handler = create_agent(
    model="openai:gpt-5.4",
    tools=[web_search, fetch],
    system_prompt="You are a careful research assistant.",
)

agent = enforce_policy(agent, policy)
```

`enforce_policy(...)` accepts either:

- a Pydantic `AgentPolicy`
- a YAML file path

That means the same agent code can stay simple in development, while deployment systems can inject policy later.

`approval_required` is special:

- if no approval handler is attached, it behaves like a graceful block
- if an approval handler is attached, the host can decide whether to allow the action at runtime
- if approval is not granted, the tool returns a structured `ok: False` result so the agent can try a fallback instead of crashing

## ✅ Approval-Required Tool Calls

Approval handlers are the bridge between static Gate 1 policy and real product interaction.

Use them when a tool should be:

- generally allowed in principle
- but only after a user, CLI host, or UI host explicitly approves the specific call

The runtime shape is intentionally small:

- `with_approval_handler(agent, handler, context_provider=...)`
- `handler` can be:
  - `True`
  - `False`
  - sync function returning `bool`
  - async function returning `bool`

Example:

```python
from fortify import (
    AgentPolicy,
    create_agent,
    edit_file,
    enforce_policy,
    read_file,
    with_approval_handler,
)

policy = AgentPolicy.model_validate(
    {
        "version": 1,
        "default_policy": {"mode": "deny"},
        "tools": {
            "read_file": {"mode": "allow"},
            "edit_file": {"mode": "approval_required"},
        },
    }
)

def approval_handler(action: dict, context: dict | None) -> bool:
    print("approval requested:", action["tool_name"], action["arguments"])
    return True

agent, handler = create_agent(
    model="openai:gpt-5.4",
    tools=[read_file, edit_file],
    system_prompt="You are a careful editor.",
)

agent = enforce_policy(agent, policy)
agent = with_approval_handler(agent, approval_handler)
```

Today the handler returns a boolean.

Future evolution:

- richer approval decisions
- interrupt / resume flows
- UI approval cards
- audit metadata on approval outcomes

That evolution is intentionally left open, but the current API is enough for CLI and simple hosted apps.

## 🚪 Gate 2: Hosted `before_action` Hooks

Gate 2 is the next layer for platform-level enforcement.

Why call this a Gate 2 primitive:

- Gate 1 answers: "is this tool allowed at all for this agent?"
- Gate 2 answers: "given who is calling, where they are deployed, and the current runtime context, should this specific action be allowed right now?"

So the design intent is:

- agent authors define tools, prompts, and behavior
- platform or admin layers inject hosted enforcement later
- runtime context flows down from the host platform, not from the tool author

The primitive is intentionally small:

- `before_action(action, context)`
- optional `context_provider()`

It runs after Gate 1 and before the real tool executes.

Example:

```python
from fortify import (
    create_agent,
    enforce_policy,
    fetch,
    web_search,
    with_before_action,
)

def before_action(action: dict, context: dict | None) -> None:
    if context is None:
        raise RuntimeError("missing runtime context")
    if context.get("tenant_id") != "acme":
        raise RuntimeError("tenant is not allowed to run this action")

def context_provider() -> dict:
    return {"tenant_id": "acme", "request_id": "req-123"}

agent, handler = create_agent(
    model="openai:gpt-5.4",
    tools=[web_search, fetch],
    system_prompt="You are a careful research assistant.",
)

agent = enforce_policy(agent, "policy.yaml")  # Gate 1
agent = with_before_action(
    agent,
    before_action=before_action,
    context_provider=context_provider,
)  # Gate 2
```

So the design split is:

- `create_agent(...)`: build the base agent
- `enforce_policy(...)`: local Gate 1 tool authorization
- `with_approval_handler(...)`: resolve `approval_required` tools at runtime
- `with_before_action(...)`: Gate 2 platform / IAM / approval integration

This is intentionally an open chantier:

- Gate 1 is already concrete and useful
- approval handlers are the first real host interaction layer
- Gate 2 starts as a tiny hook, not a full enterprise framework
- later evolution can add richer approval semantics, external policy engines, or audit sinks without bloating `create_agent(...)`

## 🧱 Workspace Sandbox

When the `bash` tool executes a command it runs inside an OS-level sandbox configured from the agent's workspace. This is filesystem + network enforcement at the kernel level — a separate concern from Gates 1/2, which decide *whether* a tool may be invoked at all.

### Runtime requirement

The `bash` tool depends on **`srt`** (Anthropic's `sandbox-runtime`). It wraps each command in `sandbox-exec` + a Seatbelt profile (macOS) or `bubblewrap` + a network namespace + a seccomp filter (Linux).

Install before using the `bash` tool:

```bash
npm install -g @anthropic-ai/sandbox-runtime
```

Supported on **macOS and Linux only** (Windows is unsupported). If `srt` is not on `PATH`, `run_command` raises `SrtUnavailableError` rather than falling back to unsandboxed execution — *fail closed by design*.

### Configuration

Tune the boundary through `LocalWorkspace`:

```python
from fortify.runtime import LocalWorkspace

workspace = LocalWorkspace(
    root_dir="./project",
    allowed_domains=["api.github.com", "*.pypi.org"],
    extra_read_paths=["/etc/ssl"],
    extra_write_paths=["/tmp/build"],
    deny_write_paths=[".env"],
    allow_unix_sockets=["/var/run/docker.sock"],
    allow_local_binding=False,
    extra_env={"NODE_ENV": "test"},
)
```

| Knob | What it controls | Default |
|---|---|---|
| `root_dir` | Workspace root; reads + writes allowed inside | required |
| `allowed_domains` | Hostnames the proxy forwards | `()` — no egress |
| `denied_domains` | Hostnames the proxy refuses | `()` |
| `extra_read_paths` | Read-only paths beyond the workspace | `()` |
| `extra_write_paths` | Writable paths beyond workspace + `/tmp` | `()` |
| `deny_write_paths` | Paths the agent can never write to | `()` |
| `allow_unix_sockets` | Unix sockets the agent can `connect()` | `()` — no IPC |
| `allow_local_binding` | Whether the agent can `bind(127.0.0.1, …)` | `False` |
| `extra_env` | Env vars passed into the sandbox | `{}` |

Defaults add up to: no network egress, no IPC sockets, no localhost bind, reads allowed inside the workspace and on system paths but not `$HOME`, writes allowed only inside the workspace + `/tmp`.

`allowUnixSockets` and `allowLocalBinding` exist because they're the two ways traffic can leave the proxy lane (Unix-domain IPC and inbound localhost). Default-deny on both; opt in per-deployment when you actually need docker-socket access, a local dev server, etc.

### Env scrubbing

The sandboxed child does **not** inherit the parent process's environment. Only an explicit allowlist passes through:

- `PATH` (curated baseline including `/opt/homebrew/bin` for Apple Silicon)
- `HOME` (set to the workspace root, so cache writes land inside `allowWrite`)
- `TMPDIR`, `TERM`
- Locale keys: `LANG`, `LC_ALL`, `LC_CTYPE`, `LC_COLLATE`, `LC_MESSAGES`
- Anything operator-supplied via `extra_env`

This means parent-process secrets — `AWS_SECRET_ACCESS_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GH_TOKEN`, `SSH_AUTH_SOCK`, etc. — **don't leak** into the agent. Tools that legitimately need credentials should receive them through `extra_env`, where you control exactly what's passed.

### Layering with Gates 1/2

| Layer | Question | Mechanism |
|---|---|---|
| **Gate 1** | Is this tool allowed at all? | `enforce_policy(...)` |
| **Approval** | Should this specific call go ahead? | `with_approval_handler(...)` |
| **Gate 2** | Should this call run *given runtime context*? | `with_before_action(...)` |
| **Sandbox** | What can the spawned shell actually do? | OS-level via `srt` |

Gate 1 decides whether the `bash` tool is callable for an agent. Approval and Gate 2 inspect each invocation. The sandbox bounds reach *if a call does run*. They're complementary — deploy whichever combination matches your threat model.

### What the sandbox does NOT do

Worth being explicit about the gaps so operators know where to layer their own checks:

- **Resource limits.** No CPU/memory/fork caps. A fork-bomb runs to completion. Use cgroups or `ulimit` if that matters.
- **Command-string semantics.** `srt` sees `sh -c "<command>"` as an opaque arg. The sandbox bounds *reach*, not intent — `rm -rf <workspace>` is permitted because the workspace is in `allowWrite`.
- **Inside-sandbox actions.** The sandbox stops the agent from exfiltrating a workspace file over the network or writing outside the boundary, but doesn't reason about what the agent does *within* the boundary.

## 🔧 Environment

Copy `.env.sample` to `.env` and set:

- `OPENAI_API_KEY`
- `LINKUP_API_KEY`
- `TAVILY_API_KEY`
- `LANGFUSE_SECRET_KEY`
- `LANGFUSE_PUBLIC_KEY`
- optional `LANGFUSE_HOST`

## 🧪 Tests

The SDK suite (367 cases) lives at `tests/` and runs with `pytest`. The platform suite (36 cases) lives at `platform/api/tests/`.

If you're already in a project virtualenv (`asianf/.venv/`):

```bash
uv run pytest tests/                                # SDK
cd platform/api && uv run pytest tests/             # platform
```

If you keep your dev env elsewhere (e.g. a `micromamba` env), point `uv` at it and pass `--active` so it doesn't try to manage `.venv` for you:

```bash
# Make uv use your current micromamba env as the project environment.
export UV_PROJECT_ENVIRONMENT=/Users/<you>/micromamba/envs/<your-env>

# First time only: pull dev-only deps (pytest-asyncio, ruff, etc.) into it.
uv sync --extra dev

# Run any uv-driven command against that env from now on.
uv run --active pytest tests/
uv run --active ruff check .
```

Without `--extra dev` you'll see *"async functions are not natively supported"* across every `@pytest.mark.asyncio` test — `pytest-asyncio` lives in the dev group and isn't installed by a plain `uv sync`. Same trap if you bring up a fresh env and forget the flag.

Drop the `UV_PROJECT_ENVIRONMENT` export into your shell rc (or a per-project `direnv` `.envrc`) if you don't want to type it every shell.

## ▶️ Run It

Install the package into your current environment:

```bash
python -m pip install -e .
```

Run the config-driven demo:

```bash
python examples/demo.py
```

Run the inline chat CLI with a local or builtin YAML agent:

```bash
fortify --agent example_agent
```

Run the CLI with code-defined agents from a Python script:

```bash
fortify --use examples/file_agents.py --agent workspace_explorer
fortify --use examples/file_agents.py --agent repo_editor
fortify --use examples/research_agents.py --agent update_researcher
fortify --use examples/research_agents.py --agent update_researcher --approval-mode ask
```

List what the CLI can currently resolve:

```bash
fortify --list-agents
```

Register a code-defined agent's manifest with the Fortify platform. `--agent`
takes a Python import path of the form `module.path:attribute`, the same shape
as ASGI/WSGI entrypoints. The CLI imports the module, grabs the agent object,
and POSTs its manifest to `${FORTIFY_API_URL}/v1/agents` using
`${FORTIFY_KEY}` as the bearer token:

```bash
fortify register --agent examples.simple_agent:agent
fortify register --agent my_app.agents:my_agent --description "Customer support bot"
```

LangGraph compiled graphs don't expose their tool nodes for inspection, so
when registering one you also need to pass an import path to the tool list:

```bash
fortify register --agent my_app.agents:graph --tools my_app.tools:my_tools
```

Supported frameworks: OpenAI Agents SDK, Google ADK, Pydantic AI, LangChain/LangGraph, Fortify agents.

## 🌐 Fortify Platform

The `platform/` directory contains an optional control plane that hosts agent definitions, dev tokens, and a live debug surface. The SDK works fully without it (`load_local_agent`, `load_builtin_agent` keep their existing semantics) — but with it you get:

- A web dashboard for editing agent YAMLs and viewing the project graph
- Mintable dev tokens (`fty_test_*`, `fty_live_*`) that authenticate the SDK
- A live Playground that streams tool calls and decisions from your running agent
- **Turn-level policy refresh** — edit YAML in the UI, the next chat picks it up

### Backend (`platform/api/`)

FastAPI over SQLite. Run with:

```bash
cd platform/api
uv run uvicorn main:app --reload --port 8000
```

The default `support-bot` project is seeded on first boot with two agents — `default` (broad access, side-effects gated by `approval_required`) and `read_only` (everything mutating denied).

Endpoints:

- `POST /v1/projects/:id/tokens` — mint a dev token (returned in full once)
- `GET /v1/projects/:id/tokens` — list dev tokens (masked)
- `DELETE /v1/projects/:id/tokens/:tid` — revoke
- `GET /v1/projects/:id/agents` — list agents with their YAMLs
- `GET /v1/projects/:id/agents/:name` — read one agent
- `PUT /v1/projects/:id/agents/:name` — save agent / policy / system YAMLs
- `WS /v1/projects/:id/serve` — producer socket (the `--serve` CLI dials here)
- `WS /v1/projects/:id/chat` — consumer socket (the dashboard Playground dials here)

DB lives at `platform/api/fortify.db`. Delete it and restart to wipe state.

### Dashboard (`platform/dashboard/`)

Vite + React + Tailwind + shadcn/ui + React Flow.

```bash
cd platform/dashboard
pnpm install        # first time
pnpm dev
```

Routes:

- `/` — overview KPIs
- `/agents` — file-tree YAML editor + live mini-graph per agent
- `/graph` — read-only project overview (everyone → agents → tools)
- `/playground` — chat with a serving agent, watch tool decisions stream live
- `/tokens` — mint, list, revoke dev tokens
- `/settings` — project settings

The dev server proxies `/v1/*` (HTTP and WebSocket) to `localhost:8000`, so HMR works through the same origin as the API.

### Serve Mode (`fortify --serve`)

Bridges your local agent runtime to the dashboard via the platform's WebSocket relay — same pattern as Cloudflare Tunnel or ngrok.

```bash
# in asianf/.env
FORTIFY_KEY=fty_test_support-bot_...
FORTIFY_AGENT_NAME=default                  # optional, defaults to "default"
FORTIFY_PROJECT_ID=support-bot              # optional, parsed from key prefix
FORTIFY_API_URL=http://localhost:8000       # optional, defaults to localhost:8000

# run
uv run fortify --serve
```

Behaviour:

- Connects `ws://${FORTIFY_API_URL}/v1/projects/${pid}/serve` with `Authorization: Bearer ${FORTIFY_KEY}`
- Sends a `hello` frame announcing the agent name (so the dashboard's "Serving" indicator can show it)
- On each inbound `chat` message, **rebuilds the runtime** (re-fetches agent + policy YAML from the platform) before running, so dashboard edits take effect at turn boundaries without a restart
- Streams every `StreamEvent` (text deltas, tool start/end, run end) back as JSON
- Auto-approves any `approval_required` tools — there's no TTY in serve mode for prompts (planned: dashboard-side approval UI)
- Reconnects with exponential backoff on socket drop

To override the agent at the CLI:

```bash
fortify --serve --agent read_only
fortify --serve --use examples/file_agents.py --agent workspace_explorer
```

### How `load_agent()` resolves with `FORTIFY_KEY`

```python
from fortify import load_agent

agent, handler = load_agent()                # → "default"
agent, handler = load_agent("read_only")     # explicit name wins
```

Resolution chain when `FORTIFY_KEY` is set:

1. `name` arg if passed
2. `FORTIFY_AGENT_NAME` env var
3. Falls back to `"default"` (always present, protected from deletion)

When `FORTIFY_KEY` is not set, `load_agent()` keeps its existing local / registered / builtin behaviour — no platform call.

## 👤 User Scope + Roles

Real backends serve many users, and different users get different capabilities. Fortify splits that into two pieces:

- **`User`** — the per-request scope. Marks "this invocation acts on behalf of alice, in role X." Async context manager; pushes a fact-bearing Biscuit through the agent runtime.
- **Role policies** — one `policy.yaml` per role, optionally inheriting from a base mixin. The runtime picks the right one at call time based on the active `User.role`.

The two are deliberately decoupled: tokens carry **identity** (who is calling), policy files carry **rules** (what they can do).

### Minimal example

```python
from fortify import User, load_fortify_agent, stream_agent

agent, handler = load_fortify_agent("support-bot")          # client + roles attached at load

async with User(user_id="alice", role="billing", ttl_seconds=300):
    async for event in stream_agent(agent, handler, "refund customer 30"):
        ...
```

That's it — no manual `attenuate_for_user`, `extract_facts`, or `ToolUseContext` plumbing at the call site. The runtime mints the per-request token, picks the `billing` role's policy file, and evaluates its constraints against each tool call.

### FastAPI middleware pattern

The cleanest production shape — set the scope once in middleware, every endpoint runs in the right user's role:

```python
from fastapi import FastAPI
from fortify import User, load_fortify_agent, stream_agent

app = FastAPI()
agent, handler = load_fortify_agent("support-bot")          # at startup

@app.middleware("http")
async def attach_user(request, call_next):
    auth = await authenticate(request)                       # your auth
    async with User(
        user_id=auth.id,
        role=auth.role,                                       # e.g. "billing"
        session_id=request.state.session_id,
        ttl_seconds=300,
    ):
        return await call_next(request)

@app.post("/chat")
async def chat(req):
    async for event in stream_agent(agent, handler, req.message):
        yield event                                          # already scoped
```

### `User` fields

| Field | Type | Required | Effect |
|---|---|---|---|
| `user_id` | `str` | ✅ | Becomes `user("alice")` in the attenuated Biscuit. |
| `role` | `str?` | optional | Becomes `role("billing")` in the Biscuit. Selects which role policy file applies at tool-call time. Fall-back: the `default` role. |
| `session_id` | `str?` | optional | Trace tagging — surfaces on Langfuse spans. |
| `user_role` | `str?` | optional | Legacy tracing alias — kept for adapter compatibility. |
| `ttl_seconds` | `int?` | optional | Embeds a `check if time($t), $t < now+ttl` predicate so the token can't outlive the request. |

### Role policies — one file per role

Agents that need per-role behaviour ship a `policies/` directory instead of a single `policy.yaml`:

```text
agent/
├── agent.yaml
├── system.md
└── policies/
    ├── default.yaml          # fallback when User.role is None / unknown
    ├── read_only.yaml        # mixin — is_mixin: true
    ├── support.yaml          # inherits: [read_only]
    └── billing.yaml          # inherits: [read_only, support]
```

Each role file is a complete `AgentPolicy`. Inheritance is left-to-right, child wins on conflicts:

```yaml
# policies/read_only.yaml  (mixin — safe base)
version: 1
is_mixin: true
default_policy:
  mode: deny
tools:
  view_orders:  { mode: allow }
  list_tickets: { mode: allow }
```

```yaml
# policies/billing.yaml
version: 1
inherits: [read_only]
tools:
  refund_order:
    mode: allow
    constraints:
      - args.amount <= 500
      - args.currency == "USD"
  wire_transfer:
    mode: approval_required
    constraints:
      - args.amount <= 100000
```

### Constraints — Rego-compatible expressions

Each tool can carry a `constraints:` list of string expressions evaluated against the call's arguments. Every constraint must pass for the call to authorize (implicit AND).

| Operator | Example | Notes |
|---|---|---|
| `==` `!=` | `args.currency == "USD"` | Strings use JSON double quotes |
| `<` `<=` `>` `>=` | `args.amount <= 500` | Type-mismatched comparisons fail-closed |
| `in` | `args.template in ["a", "b"]` | RHS must be a JSON list |
| `not in` | `args.priority not in ["urgent"]` | Two-word operator, treated as one |

Constraints intentionally look like Rego conditions — when the policy engine swaps in OPA in a later milestone, the strings carry through unchanged. To compose with AND, emit multiple lines; to compose with OR, emit two tools or two roles.

### Policy + role end-to-end

With the `billing.yaml` policy above and `async with User(user_id="alice", role="billing")`:

- `refund_order(amount=200, currency="USD")` → ✅ allowed
- `refund_order(amount=600, currency="USD")` → ❌ denied — constraint `args.amount <= 500`
- `refund_order(amount=200, currency="EUR")` → ❌ denied — constraint `args.currency == "USD"`
- `wire_transfer(amount=50000)` → ✋ requires approval (mode = `approval_required`)

Switch to `User(user_id="alice", role="default")` and `refund_order` itself is missing from the policy — falls through to the `default_policy.mode` (deny).

### Notes

- **Single-file policies still work.** A legacy `policy.yaml` is treated as the `default` role — no migration needed for agents that don't yet differentiate by role.
- **Lazy attenuation.** `User.__aenter__` only pushes a contextvar — the cryptographic work happens inside `stream_agent` / `invoke_agent` the first time the agent runs. Errors surface at first agent call, not at scope entry.
- **Local agents skip attenuation.** A `User` scope around a `load_local_agent` / `load_builtin_agent` agent logs a warning and runs with no facts. The `default` policy still applies — use `load_fortify_agent` for the full signed chain.
- **Explicit override.** Passing `tool_use_context=` explicitly to `stream_agent` / `invoke_agent` wins over an active `User` scope. Useful for tests or one-off bypass.
- **Sync callers.** `User` is async-only by design (room for KMS / audit / JWKS I/O in `__aenter__` / `__aexit__` later). From a sync context, `asyncio.run(main())` works fine.
- **Tracing vs attenuation.** `UserContext` (the legacy adapter input — three required string fields) is unchanged. `User` is a separate primitive carrying identity + role; the two coexist in the same request.

## 📡 Stream Results

For direct Python usage, the simplest runtime path is:

```python
from fortify import stream_agent

async for event in stream_agent(agent, handler, "latest AI breakthroughs"):
    ...
```

`stream_agent(...)` yields normalized events for:

- assistant text deltas
- tool lifecycle
- final run completion
