"""Hexa Bank back-office assistant, built with the OpenAI Agents SDK.

Reference for what Claude should produce in scene 2: bank_agent.py wrapped
with hexgate. Two changes only: HexgateRunner instead of Runner, and the run
bound to the caller (a teller). The policy lives on the platform.

Run (from demo/bank/):  uv run ../reference/bank_agent_secured.py "What's the balance on ACC-1042?"
"""

import asyncio
import sys

from agents import Agent, function_tool
from dotenv import load_dotenv

from _display import show_run, side_effect
from hexgate.adapters.openai import HexgateRunner
from hexgate.runtime import HexgateContext


@function_tool
def get_balance(account_id: str) -> str:
    """Get the current balance of a customer account."""
    return f"{account_id}: €48,250.00 available"


@function_tool
def list_transactions(account_id: str, limit: int = 5) -> str:
    """List the most recent transactions on a customer account."""
    return (
        f"{account_id}: 2026-11-08 salary +€4,200.00 | "
        "2026-11-05 rent -€1,450.00 | 2026-11-02 groceries -€86.40"
    )


@function_tool
def transfer_funds(from_account: str, to_iban: str, amount_eur: float, reference: str) -> str:
    """Transfer money from a customer account to an IBAN."""
    side_effect(f"💸 TRANSFER EXECUTED  €{amount_eur:,.2f}  {from_account} → {to_iban}")
    return f"Transfer of €{amount_eur:,.2f} to {to_iban} executed (ref: {reference})."


@function_tool
def close_account(account_id: str, reason: str) -> str:
    """Close a customer account permanently."""
    side_effect(f"🗑  ACCOUNT CLOSED  {account_id}")
    return f"Account {account_id} closed ({reason})."


agent = Agent(
    name="bank_assistant",
    instructions=(
        "You are the Hexa Bank back-office assistant. You help tellers handle "
        "customer requests using your tools. Be efficient: when a teller asks "
        "you to do something, do it."
    ),
    tools=[get_balance, list_transactions, transfer_funds, close_account],
    model="gpt-4o-mini",
)


async def main(message: str) -> None:
    load_dotenv()
    teller = HexgateContext(user_id="teller-042", user_roles=["teller"])
    result = await HexgateRunner().run(agent, message, hexgate_context=teller)
    show_run(result)


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:])))
