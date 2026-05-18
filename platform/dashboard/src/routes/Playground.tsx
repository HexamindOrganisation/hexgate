import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Streamdown } from 'streamdown'
import {
  Bot,
  MessageSquareCode,
  Radio,
  RadioReceiver,
  RefreshCcw,
  Send,
  User,
  Wrench,
  Check,
  X,
  CircleDashed,
  ShieldAlert,
  UserCog,
} from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { usePlayground, type ChatMessage, type ToolCall } from '@/lib/playground'
import { api, DEFAULT_PROJECT_ID, type AgentRead } from '@/lib/api'
import { parseRolesFromPolicy } from '@/lib/policy'
import { cn } from '@/lib/utils'

export function PlaygroundPage() {
  const { state, sendChat, reset } = usePlayground({ projectId: DEFAULT_PROJECT_ID })
  const [composer, setComposer] = useState('')
  const [agent, setAgent] = useState<AgentRead | null>(null)
  const [activeRole, setActiveRole] = useState<string | null>(null)
  const transcriptRef = useRef<HTMLDivElement>(null)

  // Fetch the serving agent so we know which roles are available. Roles
  // are a per-agent concept today (M1); when the dashboard later owns
  // a global role registry this useEffect moves into a shared hook.
  useEffect(() => {
    if (!state.agentName) {
      setAgent(null)
      return
    }
    let cancelled = false
    api
      .getAgent(state.agentName)
      .then((a) => {
        if (!cancelled) setAgent(a)
      })
      .catch(() => {
        if (!cancelled) setAgent(null)
      })
    return () => {
      cancelled = true
    }
  }, [state.agentName])

  const roleOptions = useMemo(
    () => (agent ? parseRolesFromPolicy(agent.policy_yaml) : []),
    [agent],
  )

  // Auto-select a sensible default when the role list changes:
  // prefer 'default', else first option, else null (single-policy agents).
  useEffect(() => {
    if (roleOptions.length === 0) {
      setActiveRole(null)
      return
    }
    setActiveRole((prev) =>
      prev && roleOptions.includes(prev) ? prev : roleOptions[0],
    )
  }, [roleOptions])

  useEffect(() => {
    transcriptRef.current?.scrollTo({
      top: transcriptRef.current.scrollHeight,
      behavior: 'smooth',
    })
  }, [state.messages])

  function submit() {
    const text = composer.trim()
    if (!text) return
    sendChat(text, activeRole ? { role: activeRole } : undefined)
    setComposer('')
  }

  return (
    <div className="-mx-8 -my-6 h-[calc(100vh-56px)] grid grid-cols-[280px_1fr_400px] overflow-hidden">
      {/* Session config */}
      <aside className="flex flex-col gap-4 border-r border-border bg-card p-5 overflow-y-auto">
        <div className="flex items-center gap-2 text-sm">
          {state.agentOnline ? (
            <>
              <span className="relative inline-flex size-2">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-allow opacity-60" />
                <span className="relative inline-flex size-2 rounded-full bg-allow" />
              </span>
              <span className="text-allow font-medium">connected</span>
            </>
          ) : (
            <>
              <span className="size-2 rounded-full bg-muted-foreground" />
              <span className="text-muted-foreground">agent offline</span>
            </>
          )}
        </div>

        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Playground</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Simulate an agent session against the active bundle.
          </p>
        </div>

        {state.agentName && (
          <div className="flex flex-col gap-1.5">
            <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
              Serving
            </div>
            <Link
              to={`/agents`}
              className="flex items-center gap-2 rounded-md border border-border bg-background px-2.5 py-1.5 text-sm font-mono hover:border-primary hover:bg-primary/5 transition-colors"
            >
              <Bot className="size-3.5 text-primary" />
              <span className="flex-1 truncate">{state.agentName}</span>
              <span className="text-[10px] text-muted-foreground">open</span>
            </Link>
          </div>
        )}

        {!state.agentOnline && (
          <div className="rounded-md border border-approval/40 bg-approval/5 p-3 text-xs leading-relaxed">
            <div className="flex items-center gap-1.5 font-medium text-approval">
              <RadioReceiver className="size-3.5" />
              No agent serving
            </div>
            <div className="mt-2 text-muted-foreground">
              Run <span className="font-mono text-foreground">fortify --serve</span> with
              your FORTIFY_KEY to expose an agent session here.
            </div>
          </div>
        )}

        {roleOptions.length > 0 && (
          <div className="flex flex-col gap-1.5">
            <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground flex items-center gap-1.5">
              <UserCog className="size-3" />
              Acting as
            </div>
            <select
              value={activeRole ?? ''}
              onChange={(e) => setActiveRole(e.target.value || null)}
              className="h-9 rounded-md border border-border bg-background px-2.5 text-sm font-mono focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              {roleOptions.map((role) => (
                <option key={role} value={role}>
                  {role}
                </option>
              ))}
            </select>
            <p className="text-[11px] text-muted-foreground leading-snug">
              Each chat turn attenuates the agent's token with{' '}
              <span className="font-mono">role(&quot;{activeRole}&quot;)</span>. The
              role's policy bundle decides which tools fire and with what
              constraints.
            </p>
          </div>
        )}

        <div className="flex flex-col gap-1.5">
          <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Session
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={reset}
            disabled={state.messages.length === 0}
            className="gap-2 justify-start"
          >
            <RefreshCcw className="size-3.5" />
            Reset session
          </Button>
        </div>

        <div className="flex flex-col gap-1.5 mt-auto">
          <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Relay status
          </div>
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Radio className={cn('size-3.5', state.connected ? 'text-allow' : 'text-muted-foreground')} />
            {state.connected ? 'relay connected' : 'reconnecting…'}
          </div>
        </div>
      </aside>

      {/* Chat transcript */}
      <section className="flex flex-col overflow-hidden">
        <header className="flex items-center justify-between px-6 py-3 border-b border-border">
          <div className="flex items-center gap-2 text-sm">
            <MessageSquareCode className="size-4 text-muted-foreground" />
            <span className="font-medium">Session</span>
            <span className="text-muted-foreground text-xs">live relay via control plane</span>
          </div>
          {activeRole && (
            <Badge
              variant="outline"
              className="gap-1.5 font-mono text-[11px] border-primary/40 text-primary"
            >
              <UserCog className="size-3" />
              acting as {activeRole}
            </Badge>
          )}
        </header>

        <div ref={transcriptRef} className="flex-1 overflow-y-auto px-6 py-6 space-y-5">
          {state.messages.length === 0 ? (
            <div className="h-full grid place-items-center text-center">
              <div className="text-sm text-muted-foreground max-w-sm">
                Send a message to start a session.
                {!state.agentOnline && (
                  <>
                    <br />
                    <span className="text-xs">(No agent connected — responses will wait.)</span>
                  </>
                )}
              </div>
            </div>
          ) : (
            state.messages.map((m) => <MessageView key={m.id} message={m} />)
          )}
        </div>

        <footer className="border-t border-border p-4">
          <div className="flex items-center gap-2">
            <input
              value={composer}
              onChange={(e) => setComposer(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  submit()
                }
              }}
              placeholder="Ask the agent to do something…"
              className="flex-1 h-10 rounded-md border border-border bg-background px-3 text-sm focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
            <Button onClick={submit} disabled={!composer.trim()} className="gap-2 h-10">
              <Send className="size-4" />
              Send
            </Button>
          </div>
        </footer>
      </section>

      {/* Decisions sidebar */}
      <aside className="flex flex-col border-l border-border overflow-hidden">
        <header className="flex items-center justify-between px-5 py-3 border-b border-border">
          <div className="flex items-center gap-2 text-sm">
            <ShieldAlert className="size-4 text-muted-foreground" />
            <span className="font-medium">Decisions</span>
          </div>
          {state.decisions.length > 0 && (
            <span className="text-xs text-muted-foreground">{state.decisions.length}</span>
          )}
        </header>
        <div className="flex-1 overflow-y-auto px-3 py-2">
          {state.decisions.length === 0 ? (
            <div className="h-full grid place-items-center text-xs text-muted-foreground text-center px-6">
              Tool-call decisions will stream here as the agent acts.
            </div>
          ) : (
            <div className="flex flex-col gap-1">
              {state.decisions.map((d) => (
                <DecisionRow key={d.id} call={d} />
              ))}
            </div>
          )}
        </div>
      </aside>
    </div>
  )
}

function MessageView({ message }: { message: ChatMessage }) {
  if (message.role === 'user') {
    return (
      <div className="flex items-start gap-3">
        <span className="size-7 rounded-full bg-primary/20 text-primary grid place-items-center text-[11px] font-medium">
          <User className="size-3.5" />
        </span>
        <div className="flex-1 pt-1">
          <div className="text-xs text-muted-foreground mb-0.5">you</div>
          <div className="text-sm whitespace-pre-wrap">{message.content}</div>
        </div>
      </div>
    )
  }

  const turn = message.turn
  return (
    <div className="flex items-start gap-3">
      <span className="size-7 rounded-full bg-secondary grid place-items-center">
        <Bot className="size-3.5 text-muted-foreground" />
      </span>
      <div className="flex-1 pt-1 space-y-3">
        <div className="text-xs text-muted-foreground">agent</div>
        {turn?.reasoning && (
          <div className="text-xs text-muted-foreground italic whitespace-pre-wrap border-l-2 border-border pl-3">
            {turn.reasoning}
          </div>
        )}
        {turn?.tools.map((t) => <ToolCallBlock key={t.id} call={t} />)}
        {message.content && (
          <div className="text-sm prose prose-sm prose-invert max-w-none">
            <Streamdown parseIncompleteMarkdown>{message.content}</Streamdown>
          </div>
        )}
        {turn?.streaming && !message.content && (
          <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <span className="size-1.5 rounded-full bg-muted-foreground animate-pulse" />
            thinking…
          </div>
        )}
        {turn?.error && (
          <div className="text-xs text-deny">error: {turn.error}</div>
        )}
      </div>
    </div>
  )
}

function ToolCallBlock({ call }: { call: ToolCall }) {
  const StateIcon =
    call.state === 'completed' ? Check : call.state === 'failed' ? X : CircleDashed
  const stateVariant: 'allow' | 'deny' | 'approval' =
    call.state === 'completed' ? 'allow' : call.state === 'failed' ? 'deny' : 'approval'
  return (
    <div className="rounded-md border border-border bg-card/50">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border">
        <Wrench className="size-3.5 text-muted-foreground" />
        <span className="font-mono text-xs">{call.name}</span>
        <Badge variant={stateVariant} className="ml-auto">
          <StateIcon className="size-3" />
          {call.state}
        </Badge>
      </div>
      {Object.keys(call.args).length > 0 && (
        <pre className="px-3 py-2 text-[11px] font-mono text-muted-foreground whitespace-pre-wrap break-words">
          {JSON.stringify(call.args, null, 2)}
        </pre>
      )}
      {call.outputSummary && (
        <div className="border-t border-border px-3 py-2 text-[11px] text-muted-foreground">
          → {call.outputSummary}
        </div>
      )}
    </div>
  )
}

function DecisionRow({ call }: { call: ToolCall }) {
  const StateIcon =
    call.state === 'completed' ? Check : call.state === 'failed' ? X : CircleDashed
  const stateColor =
    call.state === 'completed'
      ? 'text-allow'
      : call.state === 'failed'
        ? 'text-deny'
        : 'text-approval'
  return (
    <div className="rounded-md px-2.5 py-1.5 hover:bg-accent/50 text-xs">
      <div className="flex items-center gap-2">
        <StateIcon className={cn('size-3.5', stateColor)} />
        <span className="font-mono flex-1 truncate">{call.name}</span>
        <span className="text-muted-foreground text-[10px]">
          {call.endedAt ? `${call.endedAt - call.startedAt}ms` : '…'}
        </span>
      </div>
    </div>
  )
}
