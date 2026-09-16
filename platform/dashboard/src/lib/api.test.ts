/**
 * request() error-message extraction. FastAPI returns two error shapes —
 * a string `detail` (HTTPException) and a validation array `detail: [{msg}]`
 * (422). The latter used to `String()` to "[object Object]"; these lock in
 * readable messages on `ApiError.message` for every page that reads it.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "./api";

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => vi.restoreAllMocks());

describe("request() error handling", () => {
  it("joins a FastAPI 422 validation array into a readable message", async () => {
    vi.spyOn(window, "fetch").mockResolvedValue(
      jsonResponse(
        {
          detail: [
            { loc: ["body", "ban_type"], msg: "field required", type: "x" },
            { loc: ["body", "target"], msg: "must be a string", type: "y" },
          ],
        },
        422,
      ),
    );

    const err = (await api.listBans("p1").catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(422);
    expect(err.message).toBe("field required; must be a string");
    expect(err.message).not.toContain("[object Object]");
  });

  it("uses a string detail verbatim", async () => {
    vi.spyOn(window, "fetch").mockResolvedValue(
      jsonResponse({ detail: "already exists" }, 409),
    );

    const err = (await api.listBans("p1").catch((e) => e)) as ApiError;
    expect(err.message).toBe("already exists");
  });

  it("falls back to status when there's no usable detail", async () => {
    vi.spyOn(window, "fetch").mockResolvedValue(
      new Response("", { status: 500, statusText: "Internal Server Error" }),
    );

    const err = (await api.listBans("p1").catch((e) => e)) as ApiError;
    expect(err.message).toContain("500");
    expect(err.message).not.toContain("[object Object]");
  });
});

/**
 * The transcript read is the one call whose blanks are load-bearing: the
 * server reads an empty `session_id` as "this run had no session" and uses
 * it to keep the scan inside one sort-key block, so the client must send it
 * rather than drop it.
 */
describe("listLlmMessages()", () => {
  function stubOk(): string[] {
    const urls: string[] = [];
    vi.spyOn(window, "fetch").mockImplementation(async (input) => {
      urls.push(typeof input === "string" ? input : input.toString());
      return jsonResponse({ rows: [], total: 0, limit: 100, offset: 0 }, 200);
    });
    return urls;
  }

  it("sends both scopes verbatim", async () => {
    const urls = stubOk();
    await api.listLlmMessages(
      "p1",
      "sess-1",
      "11111111-1111-1111-1111-111111111111",
    );
    expect(urls[0]).toBe(
      "/v1/projects/p1/audit/llm-messages?session_id=sess-1" +
        "&run_id=11111111-1111-1111-1111-111111111111&limit=50&offset=0",
    );
  });

  it("when the session is blank then it is still sent", async () => {
    const urls = stubOk();
    await api.listLlmMessages("p1", "", "11111111-1111-1111-1111-111111111111");
    expect(urls[0]).toContain("session_id=&run_id=1111");
  });

  it("when the run id is null then it goes out blank", async () => {
    const urls = stubOk();
    await api.listLlmMessages("p1", "sess-1", null);
    expect(urls[0]).toContain("session_id=sess-1&run_id=&");
  });
});
