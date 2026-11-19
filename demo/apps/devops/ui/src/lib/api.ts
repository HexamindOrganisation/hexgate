// The Shipyard API (shipyard/server.py). A 401 anywhere means the session is
// gone: back to the sign-in page.

export interface Me {
  name: string;
  roles: string[];
  model: string;
  protected: boolean;
  error: string | null;
}

export interface Account {
  username: string;
  name: string;
  roles: string[];
}

export interface EnvState {
  version: string;
  replicas: number;
  status: string;
  error_rate: number;
  history: number[];
}

export interface Service {
  name: string;
  team: string;
  envs: Record<"staging" | "prod", EnvState>;
}

export interface ActivityEvent {
  at: number;
  kind: string;
  text: string;
  who: string;
}

export interface AppState {
  services: Service[];
  events: ActivityEvent[];
  model: string;
  protected: boolean;
  error: string | null;
}

/** One line of the /api/chat stream. */
export type ChatEvent =
  | { type: "text"; delta: string }
  | { type: "tool_start"; id: string; name: string; args: Record<string, unknown> }
  | {
      type: "tool_end";
      id: string;
      status: "ok" | "blocked" | "rejected" | "error";
      reason?: string;
      output?: string;
    }
  | {
      type: "approval";
      id: string;
      tool: string;
      args: Record<string, unknown>;
      reason?: string;
    }
  | { type: "approval_done"; id: string; approved: boolean }
  | { type: "error"; message: string }
  | { type: "done" };

function toLogin(): never {
  location.href = "/login";
  throw new Error("signed out");
}

export async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (r.status === 401) toLogin();
  return (await r.json()) as T;
}

export function postJson(url: string, body?: unknown): Promise<Response> {
  return fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

/** POST a chat message and call `onEvent` for each streamed event. */
export async function streamChat(message: string, onEvent: (ev: ChatEvent) => void): Promise<void> {
  const r = await postJson("/api/chat", { message });
  if (r.status === 401) toLogin();
  if (!r.body) return;
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i);
      buf = buf.slice(i + 1);
      if (line.trim()) onEvent(JSON.parse(line) as ChatEvent);
    }
  }
}
