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

## What You Can Import

The current curated surface is:

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
from coolagents import create_agent, agent_tool, load_builtin_agent, web_search, fetch
```

## Builtin Agents

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

## Environment

Copy `.env.sample` to `.env` and set:

- `OPENAI_API_KEY`
- `LINKUP_API_KEY`
- `TAVILY_API_KEY`
- `LANGFUSE_SECRET_KEY`
- `LANGFUSE_PUBLIC_KEY`
- optional `LANGFUSE_HOST`

## Quick start

```bash
python -m coolagents.demo
```

For the inline chat CLI:

```bash
coolagents-chat
```

If you want to install the package into your current environment first:

```bash
python -m pip install -e .
```
