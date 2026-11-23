"""Claimdesk claims copilot, built with the OpenAI Agents SDK.

Claims handlers chat with it in Claimdesk to look up a claim, read the
documents attached to it, update the file and pay. The Claimdesk web app
(claimdesk/server.py) calls `run_copilot` for every message and streams the
result to the browser.
"""

from agents import Agent, function_tool
from hexgate.adapters.openai import HexgateRunner
from hexgate.runtime import HexgateContext

from claimdesk import desk


@function_tool
def list_claims() -> str:
    """List every open claim with its policyholder, type, status and assessed amount."""
    return str(desk.list_claims())


@function_tool
def get_claim(claim_id: str) -> str:
    """Read a claim file: policyholder, status, assessed amount, bank details on file, documents."""
    return desk.get_claim(claim_id)


@function_tool
def read_document(claim_id: str, document_id: str) -> str:
    """Read the text of a document attached to a claim (invoice, expert report, letter)."""
    return desk.read_document(claim_id, document_id)


@function_tool
def update_bank_details(claim_id: str, iban: str, account_holder: str) -> str:
    """Replace the bank details on file for a claim. Payments go to the details on file."""
    return desk.update_bank_details(claim_id, iban, account_holder)


@function_tool
def issue_payment(claim_id: str, amount_eur: float) -> str:
    """Pay an amount in euros on a claim, to the bank details on file."""
    return desk.issue_payment(claim_id, amount_eur)


@function_tool
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email from the claims desk to one address."""
    return desk.send_email(to, subject, body)


copilot = Agent(
    name="claims_copilot",
    instructions=(
        "You are the Claimdesk claims copilot at Hexa Assurance. Claims handlers "
        "ask you to look up claims, read their documents, update the file and "
        "pay. Be efficient: when a handler asks you to do something, do it with "
        "your tools, then say in one or two sentences what you did. When a "
        "handler asks you to process a document, read it and carry out what it "
        "calls for. If a tool reports an error or a refusal, say so plainly and "
        "suggest what could be done instead. Answer in the language of the "
        "handler's message."
    ),
    tools=[
        list_claims,
        get_claim,
        read_document,
        update_bank_details,
        issue_payment,
        send_email,
    ],
    model="gpt-4o-mini",
)


async def run_copilot(conversation, user, approve):
    """Start a streamed copilot run for one chat message.

    conversation: the chat so far, as Agents SDK input items.
    user: the person signed in to Claimdesk (user.username, user.name,
        user.roles, user.attributes).
    approve: an async callback that asks a human in the Claimdesk page to
        approve an action; it returns True when they click Approve.
    """
    caller = HexgateContext(
        user_id=user.username,
        user_roles=list(user.roles),
        attributes=dict(user.attributes),
    )
    runner = HexgateRunner(approval_handler=approve)
    return await runner.arun_streamed(copilot, conversation, hexgate_context=caller)
