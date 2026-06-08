import { useEffect, useRef, useState } from 'react'

export type ToolCallState = 'started' | 'completed' | 'failed'
export type BlockType = 'text' | 'reasoning' | 'tool_call'

export interface RunStartEvent {
  event_type: 'run_start'
  query: string
  run_id: string
}

export interface BlockStartEvent {
  event_type: 'block_start'
  block_id: string
  block_type: BlockType
}

export interface BlockDeltaEvent {
  event_type: 'block_delta'
  block_id: string
  block_type: BlockType
  text: string
}

export interface BlockEndEvent {
  event_type: 'block_end'
  block_id: string
  block_type: BlockType
}

export interface ToolStartEvent {
  event_type: 'tool_start'
  tool_id: string
  tool_name: string
  arguments: Record<string, unknown>
}

export interface ToolEndEvent {
  event_type: 'tool_end'
  tool_id: string
  tool_name: string
  state: ToolCallState
  output_summary?: string | null
}

export interface RunEndEvent {
  event_type: 'run_end'
  result: { message: string }
}

export interface ErrorEvent {
  event_type: 'error'
  message: string
}

export type StreamEvent =
  | RunStartEvent
  | BlockStartEvent
  | BlockDeltaEvent
  | BlockEndEvent
  | ToolStartEvent
  | ToolEndEvent
  | RunEndEvent
  | ErrorEvent

export interface AgentOnlineEvent {
  type: 'agent_online'
  online: boolean
  agent?: string | null
}
export interface SessionResetEvent {
  type: 'session_reset'
}
export type ControlEvent = AgentOnlineEvent | SessionResetEvent

// ——— UI model ———

export interface ToolCall {
  id: string
  name: string
  args: Record<string, unknown>
  state: ToolCallState
  outputSummary?: string | null
  startedAt: number
  endedAt?: number
}

export interface AssistantTurn {
  id: string
  streaming: boolean
  text: string
  reasoning: string
  tools: ToolCall[]
  error?: string
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  turn?: AssistantTurn
}

export interface PlaygroundState {
  connected: boolean
  agentOnline: boolean
  agentName: string | null
  messages: ChatMessage[]
  decisions: ToolCall[]
  /** id of the assistant turn currently being streamed into. */
  currentTurnId: string | null
}

interface Options {
  projectId: string
}

export function usePlayground({ projectId }: Options) {
  const [state, setState] = useState<PlaygroundState>({
    connected: false,
    agentOnline: false,
    agentName: null,
    messages: [],
    decisions: [],
    currentTurnId: null,
  })
  const socketRef = useRef<WebSocket | null>(null)

  useEffect(() => {
    const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/v1/projects/${projectId}/chat`
    let alive = true
    let retries = 0

    function connect() {
      if (!alive) return
      const ws = new WebSocket(url)
      socketRef.current = ws

      ws.addEventListener('open', () => {
        retries = 0
        setState((s) => ({ ...s, connected: true }))
      })

      ws.addEventListener('close', () => {
        setState((s) => ({ ...s, connected: false, agentOnline: false }))
        if (alive) {
          const delay = Math.min(1000 * 2 ** retries, 15000)
          retries += 1
          setTimeout(connect, delay)
        }
      })

      ws.addEventListener('message', (evt) => {
        let payload: unknown
        try {
          payload = JSON.parse(evt.data)
        } catch {
          return
        }
        handleFrame(payload)
      })
    }

    function handleFrame(frame: unknown) {
      if (!frame || typeof frame !== 'object') return
      const f = frame as { type?: string; event_type?: string }
      if (f.type === 'agent_online') {
        const ev = f as AgentOnlineEvent
        setState((s) => ({
          ...s,
          agentOnline: Boolean(ev.online),
          agentName: ev.agent ?? (ev.online ? s.agentName : null),
        }))
        return
      }
      if (f.type === 'session_reset') return
      if (f.event_type) setState((s) => applyEvent(s, f as StreamEvent))
    }

    connect()

    return () => {
      alive = false
      socketRef.current?.close()
    }
  }, [projectId])

  /**
   * Send a chat message, optionally scoped to a role.
   *
   * When `role` is set, the platform forwards a `user_attenuation` block to
   * the dev's local `fortify --serve` process, which attenuates its parent
   * Fortify token to carry `user("playground"), role("<role>")` for this
   * turn. The role's policy bundle then drives tool authorization.
   */
  function sendChat(message: string, opts?: { role?: string | null }) {
    const ws = socketRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    const turnId = randomId()
    const userMsg: ChatMessage = {
      id: randomId(),
      role: 'user',
      content: message,
    }
    const turn: AssistantTurn = {
      id: turnId,
      streaming: true,
      text: '',
      reasoning: '',
      tools: [],
    }
    setState((s) => ({
      ...s,
      currentTurnId: turnId,
      messages: [
        ...s.messages,
        userMsg,
        { id: turn.id, role: 'assistant', content: '', turn },
      ],
    }))
    const frame: Record<string, unknown> = { type: 'chat', message }
    if (opts?.role) {
      frame.user_attenuation = {
        user: 'playground',
        role: opts.role,
        ttl_seconds: 300,
      }
    }
    ws.send(JSON.stringify(frame))
  }

  function reset() {
    const ws = socketRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'reset' }))
    }
    setState((s) => ({ ...s, currentTurnId: null, messages: [], decisions: [] }))
  }

  return { state, sendChat, reset }
}

/**
 * Pure reducer over PlaygroundState + StreamEvent.
 *
 * No external mutations, no refs. Safe under React 18 StrictMode, which
 * double-invokes state updaters to verify they're deterministic. Called
 * N times with the same input, always returns the same output.
 */
function applyEvent(state: PlaygroundState, event: StreamEvent): PlaygroundState {
  const turnId = state.currentTurnId
  if (!turnId) return state

  const msgIdx = state.messages.findIndex((m) => m.turn?.id === turnId)
  if (msgIdx < 0) return state

  const msg = state.messages[msgIdx]
  const turn = msg.turn
  if (!turn) return state

  const withTurn = (nextTurn: AssistantTurn): PlaygroundState => {
    const nextMessages = state.messages.slice()
    nextMessages[msgIdx] = { ...msg, content: nextTurn.text, turn: nextTurn }
    return { ...state, messages: nextMessages }
  }

  switch (event.event_type) {
    case 'run_start':
      return withTurn({ ...turn, streaming: true })

    case 'block_delta': {
      if (event.block_type === 'text') {
        return withTurn({ ...turn, text: turn.text + event.text })
      }
      if (event.block_type === 'reasoning') {
        return withTurn({ ...turn, reasoning: turn.reasoning + event.text })
      }
      return state
    }

    case 'tool_start': {
      if (turn.tools.some((t) => t.id === event.tool_id)) {
        return state
      }
      const call: ToolCall = {
        id: event.tool_id,
        name: event.tool_name,
        args: event.arguments,
        state: 'started',
        startedAt: Date.now(),
      }
      const nextTurn: AssistantTurn = { ...turn, tools: [...turn.tools, call] }
      const decisions = state.decisions.some((d) => d.id === call.id)
        ? state.decisions
        : [...state.decisions, call]
      return { ...withTurn(nextTurn), decisions }
    }

    case 'tool_end': {
      const idx = turn.tools.findIndex((t) => t.id === event.tool_id)
      if (idx < 0) return state
      const existing = turn.tools[idx]
      // No-op if already in the terminal state from a prior pass.
      if (
        existing.state === event.state &&
        existing.outputSummary === (event.output_summary ?? undefined)
      ) {
        return state
      }
      const updated: ToolCall = {
        ...existing,
        state: event.state,
        outputSummary: event.output_summary ?? undefined,
        endedAt: existing.endedAt ?? Date.now(),
      }
      const tools = turn.tools.slice()
      tools[idx] = updated
      const nextTurn: AssistantTurn = { ...turn, tools }
      const decisions = state.decisions.map((d) =>
        d.id === event.tool_id ? updated : d,
      )
      return { ...withTurn(nextTurn), decisions }
    }

    case 'run_end':
      return {
        ...withTurn({
          ...turn,
          streaming: false,
          text: event.result.message || turn.text,
        }),
        currentTurnId: null,
      }

    case 'error':
      return {
        ...withTurn({ ...turn, streaming: false, error: event.message }),
        currentTurnId: null,
      }

    default:
      return state
  }
}

function randomId(): string {
  return Math.random().toString(36).slice(2, 10)
}
