"""A scripted stand-in for gpt-4o-mini, for rehearsals and runs without a key.

Set TELLERDESK_MODEL=scripted. It recognizes the demo's requests and calls the
same tools the real model is expected to, in the same order, then writes a
short reply that depends on what the tools returned (done, blocked, or
rejected). Like the real model, it does what a customer's message asks for.
The app shows "scripted model" in the assistant header so it's never mistaken
for the real thing.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseTextDeltaEvent,
)

from tellerdesk import bank

Step = tuple[str, dict]

TRANSFER_WORDS = ("move", "transfer", "send")
MESSAGE_WORDS = ("handle", "message", "email")
INBOX_WORDS = ("inbox", "messages")
STATEMENT_WORD = "statement"
BALANCE_WORD = "balance"
CLOSE_WORD = "close"

ACCOUNT_ID = re.compile(r"ACC-\d+", re.IGNORECASE)
MESSAGE_ID = re.compile(r"MSG-\d+", re.IGNORECASE)
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{1,4}){3,8}\b")
SPOKEN_AMOUNT = re.compile(
    r"(\d[\d,.]*)\s*(?:euros?\b|eur\b|€)|€\s*(\d[\d,.]*)", re.IGNORECASE
)
REFERENCE = re.compile(r"reference:?\s*([^.\n]+)", re.IGNORECASE)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.\w+")
SENDER = re.compile(r"^From: .*<([^>]+)>", re.MULTILINE)
SENTENCE_END = re.compile(r"(?<=[.?!])\s+")
LAST_N = re.compile(r"last (\w+) statements", re.IGNORECASE)
MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)  # fmt: skip
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}

CURRENT_MONTH = bank.HISTORY_MONTHS[-1]  # statements are asked for the months before it
DEFAULT_ACCOUNT = "ACC-1042"
DEFAULT_REFERENCE = "Customer request"
CLOSE_REASON = "Requested by the customer"

NOT_EXECUTED = "The tool was not executed"
NEEDS_APPROVAL = "requires human approval"

WORD_DELAY_SECONDS = 0.03
HELP = "I can look up a customer's accounts, move money and handle the branch inbox. What do you need?"


def _number(digits: str) -> float | None:
    """'15,000' or '1,850.00' as a number."""
    try:
        return float(digits.replace(",", "").rstrip("."))
    except ValueError:
        return None


def _amount(text: str) -> float | None:
    """The amount in '15,000 euros' or '€850'."""
    found = SPOKEN_AMOUNT.search(text)
    return _number(found[1] or found[2]) if found else None


def _account(text: str) -> str | None:
    found = ACCOUNT_ID.search(text)
    return found[0].upper() if found else None


def _shift(month: str, by: int) -> str:
    year, number = map(int, month.split("-"))
    index = year * 12 + number - 1 + by
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _months(sentence: str) -> list[str]:
    """The statement months a sentence asks for: 'October', 'the last three'."""
    s = sentence.lower()
    last = LAST_N.search(s)
    if last:
        count = NUMBER_WORDS.get(last[1]) or (int(last[1]) if last[1].isdigit() else 1)
        return [_shift(CURRENT_MONTH, -k) for k in range(count, 0, -1)]
    year = CURRENT_MONTH[:4]
    named = [f"{year}-{i + 1:02d}" for i, name in enumerate(MONTH_NAMES) if name in s]
    return named or [_shift(CURRENT_MONTH, -1)]


def _transfer(text: str, from_account: str | None, context: str = "") -> Step | None:
    """A transfer the text asks for: an amount and an IBAN. The reference may
    be anywhere in `context`, the whole message."""
    amount, iban = _amount(text), IBAN.search(text)
    if amount is None or iban is None:
        return None
    reference = REFERENCE.search(context or text)
    args = {
        "from_account": from_account or DEFAULT_ACCOUNT,
        "to_iban": iban[0],
        "amount_eur": amount,
        "reference": reference[1].strip() if reference else DEFAULT_REFERENCE,
    }
    return ("transfer_funds", args)


def _message_steps(message: str, start: int, outputs: list[str]) -> list[Step]:
    """What a customer's message asks for, sentence by sentence.

    `start` is the index of the first step returned, so an email can carry the
    statements fetched before it."""
    sender = SENDER.search(message)
    steps: list[Step] = []
    for sentence in SENTENCE_END.split(message):
        account = _account(sentence)
        if account and STATEMENT_WORD in sentence.lower():
            fetched = []
            for month in _months(sentence):
                fetched.append(start + len(steps))
                steps.append(("get_statement", {"account_id": account, "month": month}))
            recipient = EMAIL.search(sentence)
            to = recipient[0] if recipient else (sender[1] if sender else None)
            if to:
                read = [outputs[k] for k in fetched if k < len(outputs)]
                body = "\n\n".join(o for o in read if NOT_EXECUTED not in o)
                subject = f"Statements of {account}"
                steps.append(
                    ("send_email", {"to": to, "subject": subject, "body": body})
                )
            continue
        transfer = _transfer(sentence, account, message)
        if transfer:
            steps.append(transfer)
    return steps


def _plan(message: str, outputs: list[str]) -> list[Step]:
    """Every step known so far. A step depends only on the outputs before it, so
    the list only grows as outputs come in, and a refused step doesn't stop it."""
    m = message.lower()
    account = _account(message)
    if any(w in m for w in TRANSFER_WORDS):
        transfer = _transfer(message, account)
        if transfer:
            look: Step = ("get_balance", {"account_id": transfer[1]["from_account"]})
            return [look, transfer]
    message_id = MESSAGE_ID.search(message)
    if message_id and any(w in m for w in MESSAGE_WORDS):
        read: Step = ("read_message", {"message_id": message_id[0].upper()})
        if not outputs:
            return [read]
        return [read, *_message_steps(outputs[0], 1, outputs)]
    if account and STATEMENT_WORD in m:
        return [
            ("get_statement", {"account_id": account, "month": month})
            for month in _months(message)
        ]
    if account and BALANCE_WORD in m:
        return [("get_balance", {"account_id": account})]
    if account and CLOSE_WORD in m:
        return [("close_account", {"account_id": account, "reason": CLOSE_REASON})]
    if any(w in m for w in INBOX_WORDS):
        return [("list_inbox", {})]
    return []


def _refusal(output: str, what: str) -> str | None:
    if NOT_EXECUTED not in output:
        return None
    if NEEDS_APPROVAL in output:
        return f"{what} was not approved, so I didn't do it."
    return (
        f"I couldn't do this: {what[0].lower()}{what[1:]} is not allowed by the policy."
    )


def _sentence(step: Step, output: str) -> str:
    name, args = step
    if output.startswith(bank.ERROR_PREFIX):
        return output
    if name == "transfer_funds":
        amount = bank.eur(args["amount_eur"])
        what = (
            f"The transfer of {amount} from {args['from_account']} to {args['to_iban']}"
        )
        return _refusal(output, what) or output
    if name == "send_email":
        return (
            _refusal(output, f"The email to {args['to']}") or f"I emailed {args['to']}."
        )
    if name == "get_statement":
        what = f"Reading the {args['month']} statement of {args['account_id']}"
        return _refusal(output, what) or ""
    if name == "get_balance":
        return _refusal(output, f"Looking up the balance of {args['account_id']}") or ""
    if name == "close_account":
        return _refusal(output, f"Closing {args['account_id']}") or output
    if name == "read_message":
        return _refusal(output, f"Reading {args['message_id']}") or ""
    if name == "list_inbox":
        return "The inbox has " + ", ".join(MESSAGE_ID.findall(output)) + "."
    return ""


def _summary(steps: list[Step], outputs: list[str]) -> str:
    if not steps:
        return HELP
    sentences = [s for s in map(_sentence, steps, outputs) if s]
    if sentences:
        return " ".join(sentences)
    # Only reads, all fine: show what the last one returned.
    return outputs[-1]


def _state(input: str | list) -> tuple[str, list[str]]:
    """The latest user message and the tool outputs since it."""
    items = [input] if isinstance(input, str) else list(input)
    message, outputs = "", []
    for it in items:
        if isinstance(it, str):
            message, outputs = it, []
        elif isinstance(it, dict):
            if it.get("role") == "user":
                c = it.get("content")
                message = c if isinstance(c, str) else json.dumps(c)
                outputs = []
            elif it.get("type") == "function_call_output":
                outputs.append(str(it.get("output", "")))
    return message, outputs


class ScriptedModel(Model):
    def _next(self, input: str | list) -> list:
        message, outputs = _state(input)
        steps = _plan(message, outputs)
        if len(outputs) < len(steps):
            name, args = steps[len(outputs)]
            return [
                ResponseFunctionToolCall(
                    type="function_call",
                    id=f"fc_{uuid.uuid4().hex[:10]}",
                    call_id=f"call_{uuid.uuid4().hex[:10]}",
                    name=name,
                    arguments=json.dumps(args),
                    status="completed",
                )
            ]
        return [self._text(_summary(steps, outputs))]

    @staticmethod
    def _text(text: str) -> ResponseOutputMessage:
        return ResponseOutputMessage(
            type="message",
            id=f"msg_{uuid.uuid4().hex[:10]}",
            role="assistant",
            status="completed",
            content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
        )

    async def get_response(
        self,
        system_instructions: Any,
        input: Any,
        model_settings: Any,
        tools: Any,
        output_schema: Any,
        handoffs: Any,
        tracing: Any,
        *,
        previous_response_id: Any = None,
        conversation_id: Any = None,
        prompt: Any = None,
        **kwargs: Any,
    ) -> ModelResponse:
        return ModelResponse(output=self._next(input), usage=Usage(), response_id=None)

    async def stream_response(
        self,
        system_instructions: Any,
        input: Any,
        model_settings: Any,
        tools: Any,
        output_schema: Any,
        handoffs: Any,
        tracing: Any,
        *,
        previous_response_id: Any = None,
        conversation_id: Any = None,
        prompt: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator:
        output = self._next(input)
        seq = 0
        for item in output:
            if isinstance(item, ResponseOutputMessage):
                text = item.content[0].text
                for word in re.findall(r"\S+\s*", text):
                    seq += 1
                    yield ResponseTextDeltaEvent(
                        type="response.output_text.delta",
                        item_id=item.id,
                        output_index=0,
                        content_index=0,
                        delta=word,
                        sequence_number=seq,
                        logprobs=[],
                    )
                    await asyncio.sleep(WORD_DELAY_SECONDS)
        response = Response(
            id=f"resp_{uuid.uuid4().hex[:10]}",
            created_at=time.time(),
            model="scripted",
            object="response",
            output=output,
            parallel_tool_calls=False,
            tool_choice="auto",
            tools=[],
        )
        yield ResponseCompletedEvent(
            type="response.completed", response=response, sequence_number=seq + 1
        )
