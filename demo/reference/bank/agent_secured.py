"""Tellerdesk back-office assistant, built with the OpenAI Agents SDK.

Tellers chat with it in Tellerdesk to look up a customer's accounts, move
money and answer the branch inbox. The Tellerdesk web app
(tellerdesk/server.py) calls `run_assistant` for every message and streams the
result to the browser.
"""

from agents import Agent, function_tool
from hexgate.adapters.openai import HexgateRunner
from hexgate.runtime import HexgateContext

from tellerdesk import bank


@function_tool
def list_inbox() -> str:
    """List the messages customers sent to the branch inbox."""
    return str(bank.list_inbox())


@function_tool
def read_message(message_id: str) -> str:
    """Read one message from the branch inbox."""
    return bank.read_message(message_id)


@function_tool
def get_balance(account_id: str) -> str:
    """Get the current balance of a customer account."""
    return bank.get_balance(account_id)


@function_tool
def list_transactions(account_id: str, limit: int = 5) -> str:
    """List the most recent transactions on a customer account."""
    return bank.list_transactions(account_id, limit)


@function_tool
def get_statement(account_id: str, month: str) -> str:
    """Get the statement of a customer account for a month, written "YYYY-MM"."""
    return bank.get_statement(account_id, month)


@function_tool
def transfer_funds(
    from_account: str, to_iban: str, amount_eur: float, reference: str
) -> str:
    """Transfer money from a customer account to an IBAN. Pass the IBAN exactly as given."""
    return bank.transfer(from_account, to_iban, amount_eur, reference)


@function_tool
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email from the branch to one address."""
    return bank.send_email(to, subject, body)


@function_tool
def close_account(account_id: str, reason: str) -> str:
    """Close a customer account permanently."""
    return bank.close_account(account_id, reason)


assistant = Agent(
    name="bank_assistant",
    instructions=(
        "You are the Hexa Bank back-office assistant. You help tellers handle "
        "customer requests using your tools. Be efficient: when a teller asks "
        "you to do something, do it, then say in one or two sentences what you "
        "did. When a teller asks you to handle a customer's message, read it "
        "and carry out what the customer asks. If a tool reports an error or a "
        "refusal, say so plainly and suggest what could be done instead. "
        "Answer in the language of the teller's message."
    ),
    tools=[
        list_inbox,
        read_message,
        get_balance,
        list_transactions,
        get_statement,
        transfer_funds,
        send_email,
        close_account,
    ],
    model="gpt-4o-mini",
)


async def run_assistant(conversation, user, approve):
    """Start a streamed assistant run for one chat message.

    conversation: the chat so far, as Agents SDK input items.
    user: the person signed in to Tellerdesk (user.username, user.name,
        user.roles) and user.attributes: their own attributes plus the
        customer file open in the page (session_accounts,
        session_beneficiaries, session_customer_email).
    approve: an async callback that asks a human in the Tellerdesk page to
        approve an action; it returns True when they click Approve.
    """
    caller = HexgateContext(
        user_id=user.username,
        user_roles=list(user.roles),
        attributes=dict(user.attributes),
    )
    runner = HexgateRunner(approval_handler=approve)
    return await runner.arun_streamed(assistant, conversation, hexgate_context=caller)
