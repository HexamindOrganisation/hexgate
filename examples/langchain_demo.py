import asyncio
from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from fortify.runtime import User
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
    agent = create_react_agent(llm, TOOLS)

    agent = wrap_langchain_agent(
        agent=agent,
        tools=TOOLS,  # same list passed to create_react_agent — wrapped in place
        api_key="sk-...",  # or rely on FORTIFY_KEY
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "What is the weather in Tokyo?"}]},
        user=User(
            user_id="langchain_user_1",
            role="member",
            session_id="langchain_session_1",
        ),
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
