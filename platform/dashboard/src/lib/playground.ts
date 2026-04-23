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
}
export interface SessionResetEvent {
  type: 'session_reset'
}
export interface HelloEvent {
  type: 'hello'
  agent: string
}
export type ControlEvent = AgentOnlineEvent | SessionResetEvent | HelloEvent

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
  messages: ChatMessage[]
  decisions: ToolCall[]
}

interface Options {
  projectId: string
}

export function usePlayground({ projectId }: Options) {
  const [state, setState] = useState<PlaygroundState>({
    connected: false,
    agentOnline: false,
    messages: [],
    decisions: [],
  })
  const socketRef = useRef<WebSocket | null>(null)
  const currentTurnRef = useRef<AssistantTurn | null>(null)

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
        setState((s) => ({ ...s, agentOnline: Boolean((f as AgentOnlineEvent).online) }))
        return
      }
      if (f.type === 'session_reset' || f.type === 'hello') return
      if (f.event_type) handleStreamEvent(f as StreamEvent)
    }

    function handleStreamEvent(event: StreamEvent) {
      setState((s) => applyEvent(s, event, currentTurnRef))
    }

    connect()

    return () => {
      alive = false
      socketRef.current?.close()
    }
  }, [projectId])

  function sendChat(message: string) {
    const ws = socketRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    const userMsg: ChatMessage = {
      id: randomId(),
      role: 'user',
      content: message,
    }
    const turn: AssistantTurn = {
      id: randomId(),
      streaming: true,
      text: '',
      reasoning: '',
      tools: [],
    }
    currentTurnRef.current = turn
    setState((s) => ({
      ...s,
      messages: [
        ...s.messages,
        userMsg,
        { id: turn.id, role: 'assistant', content: '', turn },
      ],
    }))
    ws.send(JSON.stringify({ type: 'chat', message }))
  }

  function reset() {
    const ws = socketRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'reset' }))
    }
    currentTurnRef.current = null
    setState((s) => ({ ...s, messages: [], decisions: [] }))
  }

  return { state, sendChat, reset }
}

function applyEvent(
  state: PlaygroundState,
  event: StreamEvent,
  turnRef: React.MutableRefObject<AssistantTurn | null>,
): PlaygroundState {
  const turn = turnRef.current
  if (!turn) return state

  const updateTurn = (patch: Partial<AssistantTurn>): PlaygroundState => {
    const next = { ...turn, ...patch }
    turnRef.current = next
    return {
      ...state,
      messages: state.messages.map((m) =>
        m.turn?.id === turn.id ? { ...m, content: next.text, turn: next } : m,
      ),
    }
  }

  switch (event.event_type) {
    case 'run_start':
      return updateTurn({ streaming: true })

    case 'block_delta': {
      if (event.block_type === 'text') {
        return updateTurn({ text: turn.text + event.text })
      }
      if (event.block_type === 'reasoning') {
        return updateTurn({ reasoning: turn.reasoning + event.text })
      }
      return state
    }

    case 'tool_start': {
      const call: ToolCall = {
        id: event.tool_id,
        name: event.tool_name,
        args: event.arguments,
        state: 'started',
        startedAt: Date.now(),
      }
      const next = { ...turn, tools: [...turn.tools, call] }
      turnRef.current = next
      return {
        ...state,
        messages: state.messages.map((m) =>
          m.turn?.id === turn.id ? { ...m, turn: next } : m,
        ),
        decisions: [...state.decisions, call],
      }
    }

    case 'tool_end': {
      const idx = turn.tools.findIndex((t) => t.id === event.tool_id)
      if (idx < 0) return state
      const updated: ToolCall = {
        ...turn.tools[idx],
        state: event.state,
        outputSummary: event.output_summary ?? undefined,
        endedAt: Date.now(),
      }
      const tools = turn.tools.slice()
      tools[idx] = updated
      const next = { ...turn, tools }
      turnRef.current = next
      return {
        ...state,
        messages: state.messages.map((m) =>
          m.turn?.id === turn.id ? { ...m, turn: next } : m,
        ),
        decisions: state.decisions.map((d) => (d.id === event.tool_id ? updated : d)),
      }
    }

    case 'run_end':
      return updateTurn({
        streaming: false,
        text: event.result.message || turn.text,
      })

    case 'error':
      return updateTurn({ streaming: false, error: event.message })

    default:
      return state
  }
}

function randomId(): string {
  return Math.random().toString(36).slice(2, 10)
}
