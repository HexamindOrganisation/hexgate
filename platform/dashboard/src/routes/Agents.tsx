import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Streamdown } from 'streamdown'
import { Bot, FileCode, FileText, ShieldCheck, Wrench } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api, type AgentRead } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
import { parseAgent } from '@/lib/policy'
import { parseRolesFromPolicy } from '@/lib/policy'

/**
 * /agents — read-only manifest view.
 *
 * Renders each agent's static identity: name, model, tool list, system
 * prompt, and the role names declared in its policy.yaml. Edit happens
 * elsewhere — policy authoring lives in /policies. Keeping this page
 * static makes the IA boundary obvious: agents = "who is this", policies
 * = "what can they do".
 *
 * Future editors for agent.yaml / system.md (when devs want to rename
 * an agent or rewrite a prompt from the UI) slot here as opt-in edit
 * affordances, but the default is inspect-only.
 */
export function AgentsPage() {
  const agents = useQuery({ queryKey: ['agents'], queryFn: () => api.listAgents() })
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null)

  useEffect(() => {
    if (!selectedAgent && agents.data && agents.data.length > 0) {
      setSelectedAgent(agents.data[0].name)
    }
  }, [agents.data, selectedAgent])

  const active = agents.data?.find((a) => a.name === selectedAgent)

  return (
    <div className="-mx-8 -my-6 h-[calc(100vh-56px)] flex flex-col overflow-hidden">
      <header className="flex items-center gap-3 px-6 py-3 border-b border-border bg-card">
        <Bot className="size-4 text-muted-foreground" />
        <span className="text-sm font-medium">Agent</span>
        <AgentPicker
          agents={agents.data ?? []}
          value={selectedAgent}
          onChange={(name) => setSelectedAgent(name)}
          loading={agents.isLoading}
        />
        <div className="flex-1" />
        {active && (
          <Link
            to="/policies"
            className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1.5"
          >
            <ShieldCheck className="size-3" />
            edit policy →
          </Link>
        )}
      </header>

      <div className="flex-1 overflow-y-auto px-8 py-6">
        {active ? <ManifestView agent={active} /> : (
          <div className="h-full grid place-items-center text-sm text-muted-foreground">
            {agents.isLoading ? 'Loading agents…' : 'No agents to inspect.'}
          </div>
        )}
      </div>
    </div>
  )
}

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
  if (loading) return <span className="text-xs text-muted-foreground">loading…</span>
  return (
    <select
      value={value ?? ''}
      onChange={(e) => onChange(e.target.value)}
      className="h-8 rounded-md border border-border bg-background px-3 text-sm font-mono focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
    >
      {agents.map((a) => (
        <option key={a.name} value={a.name}>
          {a.name}
        </option>
      ))}
    </select>
  )
}

function ManifestView({ agent }: { agent: AgentRead }) {
  const parsedManifest = parseAgent(agent.agent_yaml, agent.policy_yaml)
  const roles = parseRolesFromPolicy(agent.policy_yaml)

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      {/* Summary card */}
      <section className="rounded-lg border border-border bg-card">
        <div className="px-5 py-4 border-b border-border flex items-center gap-2">
          <Bot className="size-4 text-primary" />
          <span className="text-sm font-medium">Manifest</span>
        </div>
        <dl className="divide-y divide-border text-sm">
          <ManifestRow label="Name" value={parsedManifest?.name ?? agent.name} mono />
          <ManifestRow label="Model" value={parsedManifest?.model || '—'} mono />
          <ManifestRow label="Last updated" value={formatDate(agent.updated_at)} />
        </dl>
      </section>

      {/* Tools */}
      <section className="rounded-lg border border-border bg-card">
        <div className="px-5 py-4 border-b border-border flex items-center gap-2">
          <Wrench className="size-4 text-muted-foreground" />
          <span className="text-sm font-medium">Tools</span>
          <Badge variant="outline" className="ml-1 font-mono text-[11px]">
            {parsedManifest?.tools.length ?? 0}
          </Badge>
        </div>
        <div className="px-5 py-4">
          {parsedManifest?.tools.length ? (
            <div className="flex flex-wrap gap-1.5">
              {parsedManifest.tools.map((t) => (
                <Badge
                  key={t}
                  variant="outline"
                  className="font-mono text-[11px] py-0.5"
                >
                  {t}
                </Badge>
              ))}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">No tools declared.</p>
          )}
        </div>
      </section>

      {/* Roles */}
      <section className="rounded-lg border border-border bg-card">
        <div className="px-5 py-4 border-b border-border flex items-center gap-2">
          <ShieldCheck className="size-4 text-muted-foreground" />
          <span className="text-sm font-medium">Roles</span>
          <Badge variant="outline" className="ml-1 font-mono text-[11px]">
            {roles.length}
          </Badge>
          <div className="flex-1" />
          <Link
            to="/policies"
            className="text-[11px] text-muted-foreground hover:text-foreground"
          >
            manage in /policies →
          </Link>
        </div>
        <div className="px-5 py-4">
          {roles.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {roles.map((r) => (
                <Badge
                  key={r}
                  variant="outline"
                  className="font-mono text-[11px] py-0.5"
                >
                  {r}
                </Badge>
              ))}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              Single-policy agent — no per-role differentiation.
            </p>
          )}
        </div>
      </section>

      {/* System prompt */}
      <section className="rounded-lg border border-border bg-card">
        <div className="px-5 py-4 border-b border-border flex items-center gap-2">
          <FileText className="size-4 text-muted-foreground" />
          <span className="text-sm font-medium font-mono">system.md</span>
        </div>
        <div className="px-5 py-4 prose prose-sm prose-invert max-w-none">
          {agent.system_md.trim() ? (
            <Streamdown>{agent.system_md}</Streamdown>
          ) : (
            <p className="text-xs text-muted-foreground">No system prompt set.</p>
          )}
        </div>
      </section>

      {/* Raw agent.yaml — collapsed by default to keep the page calm */}
      <details className="rounded-lg border border-border bg-card group">
        <summary className="px-5 py-3 cursor-pointer flex items-center gap-2 text-sm font-medium select-none">
          <FileCode className="size-4 text-muted-foreground" />
          <span className="font-mono">agent.yaml</span>
          <span className="text-[10px] text-muted-foreground ml-auto group-open:hidden">
            click to expand
          </span>
        </summary>
        <pre className="px-5 py-4 text-xs font-mono whitespace-pre-wrap break-words text-foreground bg-background/40 border-t border-border">
          {agent.agent_yaml}
        </pre>
      </details>
    </div>
  )
}

function ManifestRow({
  label,
  value,
  mono,
}: {
  label: string
  value: string
  mono?: boolean
}) {
  return (
    <div className="grid grid-cols-[120px_1fr] gap-4 px-5 py-2.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={mono ? 'font-mono text-foreground' : 'text-foreground'}>
        {value}
      </dd>
    </div>
  )
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}
