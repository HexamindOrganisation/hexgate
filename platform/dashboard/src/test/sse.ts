/** AG-UI events as the assistant streams them: one `data:` frame each. */
export function sse(...events: object[]): string {
  return events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("");
}

/** A turn's response carrying `events`. */
export function sseResponse(...events: object[]): Response {
  return new Response(sse(...events), {
    headers: { "Content-Type": "text/event-stream" },
  });
}
