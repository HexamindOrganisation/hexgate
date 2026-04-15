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
    enforce_policy,
    with_before_action,
    agent_tool,
    load_agent,
    load_builtin_agent,
    register_agent,
    web_search,
    fetch,
)
```

## 🧠 Define Agents In Code

You can define agents directly in Python with `create_agent(...)`.

If you want the CLI and shared loader to resolve that agent by name, register it first and then load it through `load_agent(...)`.

A small end-to-end example registry lives in:

- `examples/agents.py`

It demonstrates:

- building one agent with `create_agent(...)` only
- building another with `create_agent(...)` plus `enforce_policy(...)`
- registering it with `register_agent(...)`
- loading it through the shared `load_agent(...)` path

For the CLI, you can import that script and then pick one of its registered agents:

```bash
coolagents-chat --use examples/agents.py --agent website_analyser
coolagents-chat --use examples/agents.py --agent news_collector
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
- `with_before_action(...)`: Gate 2 platform / IAM / approval integration

This is intentionally an open chantier:

- Gate 1 is already concrete and useful
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
coolagents-chat --use examples/agents.py --agent website_analyser
coolagents-chat --use examples/agents.py --agent news_collector
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
