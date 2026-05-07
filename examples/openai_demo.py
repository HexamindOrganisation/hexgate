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

    runner = FortifyRunner(api_key="sk-fortify-...")  # or rely on FORTIFY_KEY
    result = await runner.run(
        agent=agent,
        input="What's the weather in Cherbourg?",
        user_context=UserContext(
            user_id="openai_user_1",
            session_id="openai_session_1",
            user_role="member",
        ),
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
