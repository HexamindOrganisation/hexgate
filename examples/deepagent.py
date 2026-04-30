import asyncio
from dotenv import load_dotenv
from deepagents import create_deep_agent
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from fortify.user_context import UserContext
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

INSTRUCTIONS = (
    "You are a helpful assistant. Use the available tools to answer the user's "
    "questions. Only call destructive tools when explicitly authorized."
)


async def main():
    load_dotenv()

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    agent = create_deep_agent(
        model=llm,
        tools=TOOLS,
        system_prompt=INSTRUCTIONS,
    )

    agent = wrap_langchain_agent(
        agent=agent,
        tools=TOOLS,  # same list passed to create_deep_agent — wrapped in place
        user_context=UserContext(
            user_id="deepagent_user_1",
            user_role="member",
            session_id="deepagent_session_1",
        ),
        api_key="sk-...",  # or rely on FORTIFY_API_KEY
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "What is the weather in Tokyo?"}]}
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
