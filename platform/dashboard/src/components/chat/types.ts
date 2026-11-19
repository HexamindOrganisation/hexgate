/** The shapes the chat components render, and the single declaration of them.
 * They import from no store, so any chat surface can feed them; the
 * Playground store (lib/playground.ts) extends these rather than redeclaring. */

export interface ChatToolCall {
  id: string;
  name: string;
  args: Record<string, unknown>;
  state: "started" | "completed" | "failed";
  outputSummary?: string | null;
  /** The policy stopped it (denied, or held for approval); null when the
   * server says it ran; absent from an older server. */
  verdict?: "denied" | "held" | null;
  startedAt: number;
  endedAt?: number;
}

export interface ChatTurn {
  streaming: boolean;
  reasoning: string;
  tools: ChatToolCall[];
  error?: string;
}

export interface ChatMessageData {
  id: string;
  role: "user" | "assistant";
  content: string;
  turn?: ChatTurn;
}
