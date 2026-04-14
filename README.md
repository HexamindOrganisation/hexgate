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
- `agent_tool`
- `web_search`
- `fetch`

Example:

```python
from coolagents import create_agent, agent_tool, web_search, fetch
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
