/**
 * Copy and code for the quickstart dialog. The snippets mirror
 * docs/quickstart.mdx (chapter 4) — keep the two in sync, and keep POLICY_YAML
 * in sync with the policy there and in README.md.
 */

/** Each step's index in the dialog — Home's setup checklist opens it at
 * one. Keep in step with STEPS in QuickstartDialog.tsx. */
export const QUICKSTART_STEP = {
  idea: 0,
  key: 1,
  register: 2,
  policy: 3,
  run: 4,
  live: 5,
} as const;

export type Framework = "langchain" | "openai" | "adk" | "pydantic";

export const FRAMEWORKS: { id: Framework; label: string }[] = [
  { id: "langchain", label: "LangChain" },
  { id: "openai", label: "OpenAI Agents" },
  { id: "adk", label: "Google ADK" },
  { id: "pydantic", label: "Pydantic AI" },
];

export const POLICY_YAML = `version: 1
roles:
  support:                       # front-line: small USD refunds only
    default_policy: { mode: deny }
    tools:
      refund_order:
        mode: allow
        constraints:
          - args.amount <= 50
          - args.currency == "USD"
  billing:                       # larger refunds, major currencies
    default_policy: { mode: deny }
    tools:
      refund_order:
        mode: allow
        constraints:
          - args.amount <= 500
          - args.currency in ["USD", "EUR"]`;

export const ENV_FILE = `HEXGATE_API_KEY=fty_live_<project>_<biscuit>
OPENAI_API_KEY=sk-...
# HEXGATE_API_URL unset → the SDK talks to https://app.hexgate.ai`;

export const RUN_COMMAND = "python guarded_agent.py";

export const RUN_OUTPUT = `[support] I can't issue this refund: $400 is above the
          $50 limit for your role.
[billing] Done. I refunded 400.0 USD on order O-42.`;

export const REGISTER: Record<Framework, string> = {
  langchain: `hexgate register --agent refund_agent:graph \\
    --tools refund_agent:TOOLS --model gpt-4o-mini`,
  openai: "hexgate register --agent refund_agent:agent",
  adk: "hexgate register --agent refund_agent:agent",
  pydantic: "hexgate register --agent refund_agent:agent",
};

export const WRAP: Record<Framework, string> = {
  langchain: `import asyncio
from dotenv import load_dotenv
from hexgate.runtime import HexgateContext
from hexgate.adapters.langchain import wrap_langchain_agent

load_dotenv()
from refund_agent import graph, TOOLS, REQUEST

agent = wrap_langchain_agent(agent=graph, tools=TOOLS)  # reads HEXGATE_API_KEY

async def refund_as(role: str):
    result = await agent.ainvoke(
        REQUEST,
        hexgate_context=HexgateContext(user_id=f"{role}-agent-1", user_roles=[role]),
    )
    print(f"[{role}]", result["messages"][-1].content)

asyncio.run(refund_as("support"))
asyncio.run(refund_as("billing"))`,
  openai: `import asyncio
from dotenv import load_dotenv
from hexgate.runtime import HexgateContext
from hexgate.adapters.openai import HexgateRunner

load_dotenv()
from refund_agent import agent, REQUEST

runner = HexgateRunner()  # reads HEXGATE_API_KEY

async def refund_as(role: str):
    result = await runner.run(
        agent,
        REQUEST,
        hexgate_context=HexgateContext(user_id=f"{role}-agent-1", user_roles=[role]),
    )
    print(f"[{role}]", result.final_output)

asyncio.run(refund_as("support"))
asyncio.run(refund_as("billing"))`,
  adk: `import asyncio
from dotenv import load_dotenv
from google.adk.sessions import InMemorySessionService
from hexgate.runtime import HexgateContext
from hexgate.adapters.google import HexgateRunner

load_dotenv()
from refund_agent import agent, REQUEST

async def refund_as(role: str):
    ctx = HexgateContext(user_id=f"{role}-agent-1", session_id=f"s-{role}", user_roles=[role])
    sessions = InMemorySessionService()
    await sessions.create_session(
        app_name="refunds", user_id=ctx.user_id, session_id=ctx.session_id
    )
    runner = HexgateRunner(agent=agent, app_name="refunds", session_service=sessions)
    async for event in runner.run_async(new_message=REQUEST, hexgate_context=ctx):
        if event.is_final_response():
            print(f"[{role}]", event.content.parts[0].text)

asyncio.run(refund_as("support"))
asyncio.run(refund_as("billing"))`,
  pydantic: `from dotenv import load_dotenv
from hexgate.runtime import HexgateContext
from hexgate.adapters.pydantic_ai import wrap_pydantic_agent

load_dotenv()
from refund_agent import agent, REQUEST

guarded = wrap_pydantic_agent(agent=agent)  # reads HEXGATE_API_KEY

for role in ["support", "billing"]:
    result = guarded.run_sync(
        REQUEST,
        hexgate_context=HexgateContext(user_id=f"{role}-agent-1", user_roles=[role]),
    )
    print(f"[{role}]", result.output)`,
};
