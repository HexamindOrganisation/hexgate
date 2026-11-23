"""A scripted stand-in for gpt-4o-mini, for rehearsals and runs without a key.

Set CLAIMDESK_MODEL=scripted. It recognizes the demo's requests and calls the
same tools the real model is expected to, in the same order, then writes a
short reply that depends on what the tools returned (done, blocked, or
rejected). Like the real model, it does what a document it reads asks for. The
app shows "scripted model" in the copilot header so it's never mistaken for the
real thing.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
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

from claimdesk import desk

Step = tuple[str, dict]
FollowUp = Callable[[str, str, str], list[Step]]  # (claim id, claim file, document)

PAY_WORDS = ("pay", "settle")
INVOICE_WORDS = ("invoice", "process")
REPORT_WORDS = ("report", "summarise", "summarize")
LIST_WORDS = ("claims", "list")

CLAIM_ID = re.compile(r"CLM-\d+", re.IGNORECASE)
DOCUMENT_ID = re.compile(r"DOC-\d+")
SPOKEN_AMOUNT = re.compile(
    r"(\d[\d,.]*)\s*(?:euros?\b|eur\b|€)|€\s*(\d[\d,.]*)", re.IGNORECASE
)
TOTAL_DUE = re.compile(r"Total due: €([\d,.]+)")
FINDINGS = re.compile(r"^Findings: (.+)$", re.MULTILINE)
BANK_CHANGE_NOTICE = "bank details have changed"
NEW_BANK_DETAILS = re.compile(
    r"IBAN ([A-Z]{2}\d{2}(?: [A-Z0-9]{1,4})+), account holder ([^,.]+)"
)
FORWARD_TO = re.compile(r"forward .*? to (\S+@[\w.-]*\w)", re.IGNORECASE)

NOT_EXECUTED = "The tool was not executed"
NEEDS_APPROVAL = "requires human approval"

WORD_DELAY_SECONDS = 0.03
HELP = "I can look up claims, read their documents, update a file and pay. What do you need?"


def _number(digits: str) -> float | None:
    """'12,400' or '1,850.00' as a number."""
    try:
        return float(digits.replace(",", "").rstrip("."))
    except ValueError:
        return None


def _amount(text: str) -> float | None:
    """The amount in '12,400 euros' or '€1,850.00'."""
    found = SPOKEN_AMOUNT.search(text)
    return _number(found[1] or found[2]) if found else None


def _invoice_steps(claim_id: str, claim_file: str, invoice: str) -> list[Step]:
    """What an invoice calls for: new bank details if it announces them, then payment."""
    steps: list[Step] = []
    new_details = (
        NEW_BANK_DETAILS.search(invoice) if BANK_CHANGE_NOTICE in invoice else None
    )
    if new_details:
        steps.append(
            (
                "update_bank_details",
                {
                    "claim_id": claim_id,
                    "iban": new_details[1],
                    "account_holder": new_details[2].strip(),
                },
            )
        )
    total = TOTAL_DUE.search(invoice)
    if total:
        steps.append(
            ("issue_payment", {"claim_id": claim_id, "amount_eur": _number(total[1])})
        )
    return steps


def _report_steps(claim_id: str, claim_file: str, report: str) -> list[Step]:
    """What a report calls for: forwarding the claim file, if it asks to."""
    recipient = FORWARD_TO.search(report)
    if recipient is None:
        return []
    email = {
        "to": recipient[1],
        "subject": f"Claim file {claim_id}",
        "body": claim_file,
    }
    return [("send_email", email)]


def _document_steps(claim_id: str, outputs: list[str], follow: FollowUp) -> list[Step]:
    """Read the claim's latest document, then do what the document calls for.

    `outputs` are the results so far: [0] the claim file, [1] the document."""
    documents = DOCUMENT_ID.findall(outputs[0]) if outputs else []
    if not documents:
        return []
    read: Step = ("read_document", {"claim_id": claim_id, "document_id": documents[-1]})
    if len(outputs) < 2:
        return [read]
    return [read, *follow(claim_id, outputs[0], outputs[1])]


def _plan(message: str, outputs: list[str]) -> list[Step]:
    """Every step known so far. A step depends only on the outputs before it, so
    the list only grows as outputs come in, and a refused step doesn't stop it."""
    m = message.lower()
    claim = CLAIM_ID.search(message)
    if claim is None:
        return [("list_claims", {})] if any(w in m for w in LIST_WORDS) else []
    claim_id = claim[0].upper()
    look: Step = ("get_claim", {"claim_id": claim_id})
    amount = _amount(message)
    if amount is not None and any(w in m for w in PAY_WORDS):
        return [look, ("issue_payment", {"claim_id": claim_id, "amount_eur": amount})]
    if any(w in m for w in INVOICE_WORDS):
        return [look, *_document_steps(claim_id, outputs, _invoice_steps)]
    if any(w in m for w in REPORT_WORDS):
        return [look, *_document_steps(claim_id, outputs, _report_steps)]
    return [look]


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
    claim_id = args.get("claim_id", "")
    if output.startswith(desk.ERROR_PREFIX):
        return output
    if name == "issue_payment":
        amount = desk.eur(args["amount_eur"])
        return _refusal(output, f"The payment of {amount} on {claim_id}") or output
    if name == "update_bank_details":
        return (
            _refusal(output, f"The change of bank details on {claim_id}")
            or f"I updated the bank details on {claim_id} as the invoice asks."
        )
    if name == "send_email":
        return (
            _refusal(output, f"The email to {args['to']}")
            or f"I forwarded the claim file to {args['to']} as the report asks."
        )
    if name == "read_document":
        refused = _refusal(output, f"Reading {args['document_id']} on {claim_id}")
        if refused:
            return refused
        findings = FINDINGS.search(output)
        return f"Summary of {args['document_id']}: {findings[1]}" if findings else ""
    if name == "get_claim":
        return _refusal(output, f"Opening {claim_id}") or ""
    if name == "list_claims":
        return "The open claims are " + ", ".join(CLAIM_ID.findall(output)) + "."
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
