"""Minimal OpenAI-compatible mock for the demo smoke test.

Returns a canned assistant reply for /v1/chat/completions (streaming and
non-streaming) so the smoke test can exercise the full agent path without a real
provider key. Not used at runtime — test-only.
"""

import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
REPLY = "Hello from the mock LLM! The plumbing works."


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    model = body.get("model", "gpt-4o-mini")
    if body.get("stream"):
        def gen():
            yield _sse({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}], "model": model})
            for word in REPLY.split():
                yield _sse({"choices": [{"index": 0, "delta": {"content": word + " "}, "finish_reason": None}], "model": model})
            yield _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "model": model})
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    return JSONResponse({
        "id": "chatcmpl-mock", "object": "chat.completion", "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": REPLY}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 8, "total_tokens": 9},
    })


def _sse(obj):
    obj.setdefault("id", "chatcmpl-mock")
    obj.setdefault("object", "chat.completion.chunk")
    obj.setdefault("created", int(time.time()))
    return f"data: {json.dumps(obj)}\n\n"
