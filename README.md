# coolagents

`coolagents` is a lightweight LangChain-based agent runtime built around:

- `langchain`
- `gpt-5.4`
- `Linkup` web search
- Tavily-based page fetch
- `Langfuse` tracing

This package is intentionally small. The first milestone is a single assistant with:

- `web_search`
- `fetch`

## ⚡ Quick Start

If you just want to install `coolagents` and try the CLI quickly:

1. Install the package in editable mode.
2. Copy the sample environment file.
3. Fill in the required API keys.
4. Run the chat CLI against the included local example agent.

```bash
python -m pip install -e .
cp .env.sample .env
coolagents-chat --agent example_agent
```

Required keys for the example CLI flow:

- `OPENAI_API_KEY`
- `LINKUP_API_KEY`
- `TAVILY_API_KEY`

Useful next commands:

```bash
coolagents-chat --list-agents
coolagents-chat --agent researcher
coolagents-chat --use examples/file_agents.py --agent workspace_explorer
coolagents-chat --use examples/research_agents.py --agent update_researcher
```

The included local agent lives in `example_agent/`, and the CLI can also load:

- builtin packaged agents like `researcher`
- code-defined agents registered from `examples/file_agents.py`
- code-defined research agents registered from `examples/research_agents.py`

## ✨ Core Primitives

The two main primitives are:

- `create_agent(...)`
- `@agent_tool(...)`

Use them when you want to define everything directly in Python.

```python
from coolagents import agent_tool, create_agent


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
- `agent_tool`
- `web_search`
- `fetch`

Example:

```python
from coolagents import (
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
    register_agent,
    fetch,
    web_search,
)
```

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
coolagents-chat --use examples/file_agents.py --agent workspace_explorer
coolagents-chat --use examples/file_agents.py --agent repo_editor
coolagents-chat --use examples/research_agents.py --agent update_researcher
```

## 🗂️ Builtin And Local Agents

The package now ships with a small `coolagents.builtin_agents` directory for official starter agents.

Current builtin agents:

- `researcher`

Example:

```python
from coolagents import load_builtin_agent

agent, handler = load_builtin_agent("researcher")
```

The CLI also discovers local agents from:

- `./<agent_dir>/agent.yaml`
- `./agents/<agent_dir>/agent.yaml`

This repo includes a root-level `example_agent/` directory, so from the project root you can simply run:

```bash
coolagents-chat --agent example_agent
```

## 🔐 Policy Shape

Policies are intentionally simple for now:

```yaml
version: 1

default_policy:
  mode: deny

tools:
  web_search:
    mode: allow
  fetch:
    mode: allow
```

Supported modes:

- `allow`
- `deny`
- `approval_required`

## 🛡️ Gate 1: Local Policy Enforcement

Gate 1 is the current built-in security layer.

Use it when:

- developers should be able to build and test agents freely
- a host platform or admin layer wants to constrain tool access later
- you want deny-by-default behavior before a tool actually runs

`create_agent(...)` stays close to LangChain. Policy enforcement is applied after agent creation:

```python
from coolagents import AgentPolicy, create_agent, enforce_policy, fetch, web_search

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
from coolagents import (
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
from coolagents import (
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

## 🔧 Environment

Copy `.env.sample` to `.env` and set:

- `OPENAI_API_KEY`
- `LINKUP_API_KEY`
- `TAVILY_API_KEY`
- `LANGFUSE_SECRET_KEY`
- `LANGFUSE_PUBLIC_KEY`
- optional `LANGFUSE_HOST`

## ▶️ Run It

Install the package into your current environment:

```bash
python -m pip install -e .
```

Run the config-driven demo:

```bash
python -m coolagents.demo
```

Run the inline chat CLI with a local or builtin YAML agent:

```bash
coolagents-chat --agent example_agent
```

Run the CLI with code-defined agents from a Python script:

```bash
coolagents-chat --use examples/file_agents.py --agent workspace_explorer
coolagents-chat --use examples/file_agents.py --agent repo_editor
coolagents-chat --use examples/research_agents.py --agent update_researcher
coolagents-chat --use examples/research_agents.py --agent update_researcher --approval-mode ask
```

List what the CLI can currently resolve:

```bash
coolagents-chat --list-agents
```

## 📡 Stream Results

For direct Python usage, the simplest runtime path is:

```python
from coolagents import stream_agent

async for event in stream_agent(agent, handler, "latest AI breakthroughs"):
    ...
```

`stream_agent(...)` yields normalized events for:

- assistant text deltas
- tool lifecycle
- final run completion
