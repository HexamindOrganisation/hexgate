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
from coolagents import AgentPolicy, agent_tool, create_agent


@agent_tool(name="my_lookup")
async def my_lookup(query: str) -> dict:
    """Look up something useful."""
    return {"query": query, "results": []}


policy = AgentPolicy.model_validate(
    {
        "version": 1,
        "default_policy": {"mode": "deny"},
        "tools": {"my_lookup": {"mode": "allow"}},
    }
)

agent, handler = create_agent(
    model="openai:gpt-5.4",
    tools=[my_lookup],
    system_prompt="You are a helpful research assistant.",
    policy=policy,
)
```

`create_agent(...)` accepts policy as either:

- a Pydantic `AgentPolicy`
- or a YAML file path

## 📦 What You Can Import

The current curated surface includes:

- `create_agent`
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

- defining a policy in Python
- building an agent with `create_agent(...)`
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
