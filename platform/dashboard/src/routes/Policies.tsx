import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Bot,
  FileCode,
  Network,
  Save,
  ShieldCheck,
} from 'lucide-react'
import { api, type PolicyValidationError } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'

type Tab = 'yaml' | 'graph'

const TAB_LABEL: Record<Tab, string> = {
  yaml: 'YAML',
  graph: 'Graph',
}

const TAB_ICON: Record<Tab, typeof FileCode> = {
  yaml: FileCode,
  graph: Network,
}

export function PoliciesPage() {
  const agents = useQuery({ queryKey: ['agents'], queryFn: () => api.listAgents() })
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('yaml')

  // Auto-select the first agent once the list loads.
  useEffect(() => {
    if (!selectedAgent && agents.data && agents.data.length > 0) {
      setSelectedAgent(agents.data[0].name)
    }
  }, [agents.data, selectedAgent])

  return (
    <div className="-mx-8 -my-6 h-[calc(100vh-56px)] flex flex-col overflow-hidden">
      {/* Top bar: agent picker + tabs */}
      <header className="flex items-center justify-between gap-4 px-6 py-3 border-b border-border bg-card">
        <div className="flex items-center gap-3">
          <ShieldCheck className="size-4 text-muted-foreground" />
          <span className="text-sm font-medium">Policy</span>
          <AgentPicker
            agents={agents.data ?? []}
            value={selectedAgent}
            onChange={(name) => setSelectedAgent(name)}
            loading={agents.isLoading}
          />
        </div>
        <Tabs value={tab} onChange={setTab} />
      </header>

      {/* Content */}
      <div className="flex-1 overflow-hidden">
        {selectedAgent ? (
          tab === 'yaml' ? (
            <YamlEditor agentName={selectedAgent} />
          ) : (
            <GraphPlaceholder />
          )
        ) : (
          <div className="h-full grid place-items-center text-sm text-muted-foreground">
            {agents.isLoading ? 'Loading agents…' : 'No agents to select.'}
          </div>
        )}
      </div>
    </div>
  )
}

/**
 * Lightweight tab strip used in /policies. Two tabs today (YAML, Graph);
 * the M2 Rego adapter will land a third here without restructuring.
 */
function Tabs({
  value,
  onChange,
}: {
  value: Tab
  onChange: (t: Tab) => void
}) {
  return (
    <div className="flex items-center gap-1 rounded-md border border-border bg-background p-0.5">
      {(['yaml', 'graph'] as const).map((t) => {
        const Icon = TAB_ICON[t]
        const active = value === t
        return (
          <button
            key={t}
            onClick={() => onChange(t)}
            className={cn(
              'flex items-center gap-1.5 rounded px-3 py-1 text-xs font-medium transition-colors',
              active
                ? 'bg-primary text-primary-foreground'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            <Icon className="size-3" />
            {TAB_LABEL[t]}
          </button>
        )
      })}
    </div>
  )
}

/**
 * Agent picker — plain native <select>, styled to match the rest of the
 * dashboard's controls. A dropdown is enough today (≤5 agents per project);
 * if a project grows past ~20 agents this becomes a combobox/typeahead.
 */
function AgentPicker({
  agents,
  value,
  onChange,
  loading,
}: {
  agents: { name: string }[]
  value: string | null
  onChange: (name: string) => void
  loading: boolean
}) {
  if (loading) {
    return <span className="text-xs text-muted-foreground">loading…</span>
  }
  return (
    <div className="flex items-center gap-2 text-sm">
      <span className="text-muted-foreground text-xs">Agent:</span>
      <div className="relative">
        <Bot className="absolute left-2 top-1/2 -translate-y-1/2 size-3.5 text-muted-foreground pointer-events-none" />
        <select
          value={value ?? ''}
          onChange={(e) => onChange(e.target.value)}
          className="h-8 rounded-md border border-border bg-background pl-7 pr-3 text-sm font-mono focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          {agents.map((a) => (
            <option key={a.name} value={a.name}>
              {a.name}
            </option>
          ))}
        </select>
      </div>
    </div>
  )
}

/**
 * YAML editor pane — wraps the canonical ``policy.yaml`` of the selected
 * agent. Same primitive the /agents page used to host inline; relocated
 * here as the write-path for policies. The /agents page now only shows
 * manifest data, no policy editing.
 */
function YamlEditor({ agentName }: { agentName: string }) {
  const qc = useQueryClient()
  const agent = useQuery({
    queryKey: ['agent', agentName],
    queryFn: () => api.getAgent(agentName),
  })

  const [draft, setDraft] = useState<string>('')
  const [dirty, setDirty] = useState(false)
  const [errors, setErrors] = useState<PolicyValidationError[] | null>(null)

  // Re-sync the draft when we switch agents or the server copy refreshes.
  useEffect(() => {
    if (!agent.data) return
    setDraft(agent.data.policy_yaml)
    setDirty(false)
    setErrors(null)
  }, [agent.data])

  const saveMutation = useMutation({
    mutationFn: () => api.updateAgent(agentName, { policy_yaml: draft }),
    onSuccess: () => {
      setDirty(false)
      qc.invalidateQueries({ queryKey: ['agent', agentName] })
      qc.invalidateQueries({ queryKey: ['agents'] })
    },
  })

  const validateMutation = useMutation({
    mutationFn: () => api.validatePolicy(agentName, draft),
    onSuccess: (resp) => setErrors(resp.errors),
  })

  const originalSource = agent.data?.policy_yaml ?? ''

  return (
    <div className="h-full flex flex-col">
      <header className="flex items-center justify-between gap-2 px-6 py-2 border-b border-border">
        <div className="flex items-center gap-2 text-sm">
          <FileCode className="size-3.5 text-muted-foreground" />
          <span className="font-mono">{agentName}/policy.yaml</span>
          {dirty && <Badge variant="approval">unsaved</Badge>}
        </div>
        <div className="flex items-center gap-1.5">
          <Button
            size="sm"
            variant="outline"
            onClick={() => validateMutation.mutate()}
            disabled={validateMutation.isPending}
            className="gap-1.5 h-8"
          >
            <ShieldCheck className="size-3.5" />
            {validateMutation.isPending ? 'Checking…' : 'Validate'}
          </Button>
          <Button
            size="sm"
            onClick={() => saveMutation.mutate()}
            disabled={!dirty || saveMutation.isPending}
            className="gap-2 h-8"
          >
            <Save className="size-3.5" />
            {saveMutation.isPending ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </header>
      {errors && (
        <div
          className={cn(
            'px-6 py-2 text-xs border-b',
            errors.length === 0
              ? 'bg-allow/5 border-allow/30 text-allow'
              : 'bg-deny/5 border-deny/30 text-deny',
          )}
        >
          {errors.length === 0 ? (
            <span>Policy parses cleanly.</span>
          ) : (
            <ul className="space-y-0.5 font-mono">
              {errors.map((err, i) => (
                <li key={i}>
                  {err.role && <span className="text-foreground">{err.role}</span>}
                  {err.role && err.line ? ':' : ''}
                  {err.line ? err.line : ''}
                  {err.role || err.line ? ' — ' : ''}
                  {err.message}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      <textarea
        value={draft}
        onChange={(e) => {
          setDraft(e.target.value)
          setDirty(e.target.value !== originalSource)
          if (errors) setErrors(null)
        }}
        spellCheck={false}
        className="flex-1 resize-none bg-background p-6 font-mono text-sm leading-relaxed text-foreground focus:outline-none"
      />
    </div>
  )
}

/**
 * Graph tab placeholder — the node-edge visualization lands in commit 2/3.
 * Keeps the tab present today so the IA is stable.
 */
function GraphPlaceholder() {
  return (
    <div className="h-full grid place-items-center text-center px-8">
      <div className="max-w-md space-y-2">
        <Network className="size-8 text-muted-foreground mx-auto" />
        <p className="text-sm text-muted-foreground">
          Graph view coming next — node-edge visualization of roles → tools
          colored by mode, with inheritance edges between role nodes.
        </p>
      </div>
    </div>
  )
}
