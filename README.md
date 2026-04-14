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
cd coolagents
python -m coolagents.demo
```
