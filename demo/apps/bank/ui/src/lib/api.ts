// The Tellerdesk API (tellerdesk/server.py). A 401 anywhere means the session is
// gone: back to the sign-in page.

export interface Account {
  username: string;
  name: string;
  roles: string[];
  /** Caller attributes from users.yaml (the open customer file is added per run). */
  attributes: Record<string, unknown>;
}

export interface Me extends Account {
  open_customer: string;
  model: string;
  protected: boolean;
  error: string | null;
}

export interface BankAccount {
  id: string;
  kind: "current" | "savings";
  balance_eur: number;
  closed: boolean;
  /** What went wrong with a transfer, as the bank sees it. */
  flags: string[];
}

export interface Beneficiary {
  name: string;
  iban: string;
}

export interface Customer {
  id: string;
  name: string;
  email: string;
  accounts: BankAccount[];
  beneficiaries: Beneficiary[];
}

export interface Message {
  id: string;
  from: string;
  subject: string;
  preview: string;
}

export interface ActivityEvent {
  at: number;
  kind: string;
  text: string;
  who: string;
}

export interface AppState {
  customers: Customer[];
  inbox: Message[];
  open_customer: string;
  events: ActivityEvent[];
  moved_out_eur: number;
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
