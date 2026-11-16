"""A scripted stand-in for gpt-4o-mini, for rehearsals and runs without a key.

Set SHIPYARD_MODEL=scripted. It recognizes the demo's requests and calls the
same tools the real model would, in the same order, then writes a short reply
that depends on what the tools returned (done, blocked, or rejected). The app
shows "scripted model" in the copilot header so it's never mistaken for the
real thing.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator

from agents.models.interface import Model
from agents.items import ModelResponse
from agents.usage import Usage
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseTextDeltaEvent,
)

RC = "v2.4.0-rc1"


def _plan(message: str, previous: str = "") -> list[tuple[str, dict]]:
    m = message.lower()
    # A follow-up that only names the other environment ("OK, staging then.")
    # repeats the previous request there.
    if previous and not any(w in m for w in ("ship", "deploy", "release", "restart", "scale", "log", "status", "delete")):
        for env, other in (("staging", "prod"), ("prod", "staging")):
            if env in m:
                return _plan(previous.lower().replace(other, env))
    service = next((s for s in ("payments", "checkout", "notifications", "api-gateway") if s in m), "payments")
    version = (re.search(r"v\d+\.\d+\.\d+(?:-\w+)?", message) or [RC])[0]
    if "delete" in m:
        env = "prod" if "prod" in m else "staging"
        return [("delete_environment", {"env": env})]
    if any(w in m for w in ("ship", "deploy", "roll out", "release")):
        env = "staging" if "staging" in m else "prod"
        steps = []
        if "error" in m or "log" in m:
            steps.append(("get_logs", {"service": service, "env": env}))
        steps.append(("deploy_version", {"service": service, "version": version, "env": env}))
        if "restart" in m:
            steps.append(("restart_service", {"service": service, "env": env}))
        return steps
    if "restart" in m:
        return [("restart_service", {"service": service, "env": "staging" if "staging" in m else "prod"})]
    if "scale" in m:
        n = int((re.search(r"\b(\d+)\b", message) or [0, 4])[1])
        return [("scale_service", {"service": service, "env": "staging" if "staging" in m else "prod", "replicas": n})]
    if "log" in m:
        return [("get_logs", {"service": service, "env": "staging" if "staging" in m else "prod"})]
    if any(w in m for w in ("status", "services", "health", "how is", "how's")):
        return [("list_services", {})]
    return []


def _summary(steps: list[tuple[str, dict]], outputs: list[str]) -> str:
    if not steps:
        return "I can check services, read logs, restart, scale and deploy. What do you need?"
    name, args = steps[-1]
    last = outputs[-1] if outputs else ""
    if "requires human approval" in last:
        return f"The {name.replace('_', ' ')} was not approved, so I didn't run it. Nothing changed."
    if "denied by the agent policy" in last:
        if name == "deploy_version":
            return (
                f"I couldn't deploy {args['version']} to {args['env']}: your role isn't allowed to. "
                "I can deploy it to staging instead, or a release manager can do the prod deploy."
            )
        return f"I couldn't run {name.replace('_', ' ')}: it's not allowed for your role. Nothing changed."
    if name == "deploy_version":
        return f"Done: {args['service']} {args['version']} is rolling out to {args['env']}."
    if name == "delete_environment":
        return f"Done: the {args['env']} environment is deleted."
    if name == "list_services":
        return "Everything is healthy except payments in prod, which shows elevated errors."
    if name == "get_logs":
        return "The logs show upstream timeouts to the card processor."
    return "Done."


def _state(input) -> tuple[str, str, list[str]]:
    """The latest user message, the one before it, and the tool outputs since the latest."""
    items = [input] if isinstance(input, str) else list(input)
    message, previous, outputs = "", "", []
    for it in items:
        if isinstance(it, str):
            message, previous, outputs = it, message, []
        elif isinstance(it, dict):
            if it.get("role") == "user":
                c = it.get("content")
                message, previous = (c if isinstance(c, str) else json.dumps(c)), message
                outputs = []
            elif it.get("type") == "function_call_output":
                outputs.append(str(it.get("output", "")))
    return message, previous, outputs


class ScriptedModel(Model):
    def _next(self, input) -> list:
        message, previous, outputs = _state(input)
        steps = _plan(message, previous)
        if len(outputs) < len(steps):
            # Stop early when the previous step was refused.
            if outputs and "The tool was not executed" in outputs[-1]:
                return [self._text(_summary(steps[: len(outputs)], outputs))]
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

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema,
                           handoffs, tracing, *, previous_response_id=None, conversation_id=None,
                           prompt=None, **kwargs) -> ModelResponse:
        return ModelResponse(output=self._next(input), usage=Usage(), response_id=None)

    async def stream_response(self, system_instructions, input, model_settings, tools, output_schema,
                              handoffs, tracing, *, previous_response_id=None, conversation_id=None,
                              prompt=None, **kwargs) -> AsyncIterator:
        output = self._next(input)
        seq = 0
        for item in output:
            if isinstance(item, ResponseOutputMessage):
                text = item.content[0].text
                for word in re.findall(r"\S+\s*", text):
                    seq += 1
                    yield ResponseTextDeltaEvent(
                        type="response.output_text.delta", item_id=item.id, output_index=0,
                        content_index=0, delta=word, sequence_number=seq, logprobs=[],
                    )
                    await asyncio.sleep(0.03)
        response = Response(
            id=f"resp_{uuid.uuid4().hex[:10]}", created_at=time.time(), model="scripted",
            object="response", output=output, parallel_tool_calls=False, tool_choice="auto", tools=[],
        )
        yield ResponseCompletedEvent(type="response.completed", response=response, sequence_number=seq + 1)
