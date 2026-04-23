import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ReactFlow, Background, BackgroundVariant } from '@xyflow/react'
import { Bot, FileCode, FileText, Save, AlertTriangle } from 'lucide-react'
import { api, type AgentRead } from '@/lib/api'
import { buildAgentGraph } from '@/lib/graph'
import { nodeTypes } from '@/components/graph/nodes'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'

type FileKind = 'policy_yaml' | 'agent_yaml' | 'system_md'

const FILE_LABEL: Record<FileKind, string> = {
  policy_yaml: 'policy.yaml',
  agent_yaml: 'agent.yaml',
  system_md: 'system.md',
}

const FILE_ICON: Record<FileKind, typeof FileCode> = {
  policy_yaml: FileCode,
  agent_yaml: FileCode,
  system_md: FileText,
}

export function AgentsPage() {
  const agents = useQuery({ queryKey: ['agents'], queryFn: () => api.listAgents() })

  const [selectedAgent, setSelectedAgent] = useState<string | null>(null)
  const [selectedFile, setSelectedFile] = useState<FileKind>('policy_yaml')

  // When the list loads for the first time, auto-select the first agent.
  useEffect(() => {
    if (!selectedAgent && agents.data && agents.data.length > 0) {
      setSelectedAgent(agents.data[0].name)
    }
  }, [agents.data, selectedAgent])

  return (
    <div className="-mx-8 -my-6 h-[calc(100vh-56px)] grid grid-cols-[260px_1fr_1fr] overflow-hidden">
      {/* File tree */}
      <aside className="border-r border-border bg-card overflow-y-auto">
        <div className="px-4 py-3 border-b border-border">
          <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Agents
          </div>
        </div>
        {agents.isLoading ? (
          <div className="p-4 text-xs text-muted-foreground">Loading…</div>
        ) : (
          <div className="py-2">
            {agents.data?.map((a) => (
              <AgentTreeItem
                key={a.name}
                agent={a}
                expanded={selectedAgent === a.name}
                selectedFile={selectedAgent === a.name ? selectedFile : null}
                onSelect={(file) => {
                  setSelectedAgent(a.name)
                  setSelectedFile(file)
                }}
              />
            ))}
          </div>
        )}
      </aside>

      {selectedAgent ? (
        <AgentWorkspace
          agentName={selectedAgent}
          file={selectedFile}
        />
      ) : (
        <div className="col-span-2 grid place-items-center text-sm text-muted-foreground">
          Select an agent to edit.
        </div>
      )}
    </div>
  )
}

function AgentTreeItem({
  agent,
  expanded,
  selectedFile,
  onSelect,
}: {
  agent: AgentRead
  expanded: boolean
  selectedFile: FileKind | null
  onSelect: (file: FileKind) => void
}) {
  return (
    <div>
      <button
        onClick={() => onSelect('policy_yaml')}
        className={cn(
          'flex w-full items-center gap-2 px-4 py-1.5 text-sm transition-colors',
          expanded ? 'text-foreground' : 'text-muted-foreground hover:text-foreground',
        )}
      >
        <Bot className="size-3.5" />
        <span className="font-medium">{agent.name}</span>
      </button>
      {expanded && (
        <div className="pb-1">
          {(['policy_yaml', 'agent_yaml', 'system_md'] as const).map((kind) => {
            const Icon = FILE_ICON[kind]
            return (
              <button
                key={kind}
                onClick={() => onSelect(kind)}
                className={cn(
                  'flex w-full items-center gap-2 pl-10 pr-4 py-1 text-xs transition-colors',
                  selectedFile === kind
                    ? 'bg-primary/10 text-primary'
                    : 'text-muted-foreground hover:text-foreground',
                )}
              >
                <Icon className="size-3" />
                <span className="font-mono">{FILE_LABEL[kind]}</span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

function AgentWorkspace({ agentName, file }: { agentName: string; file: FileKind }) {
  const qc = useQueryClient()
  const agent = useQuery({
    queryKey: ['agent', agentName],
    queryFn: () => api.getAgent(agentName),
  })

  const [draft, setDraft] = useState<string>('')
  const [dirty, setDirty] = useState(false)

  // Sync draft from server when agent or file changes
  useEffect(() => {
    if (!agent.data) return
    setDraft(agent.data[file])
    setDirty(false)
  }, [agent.data, file])

  const saveMutation = useMutation({
    mutationFn: (patch: { agent_yaml?: string; policy_yaml?: string; system_md?: string }) =>
      api.updateAgent(agentName, patch),
    onSuccess: () => {
      setDirty(false)
      qc.invalidateQueries({ queryKey: ['agent', agentName] })
      qc.invalidateQueries({ queryKey: ['agents'] })
    },
  })

  // Build a live preview agent by combining the server copy with the unsaved draft
  const previewAgent = useMemo<AgentRead | null>(() => {
    if (!agent.data) return null
    return { ...agent.data, [file]: draft }
  }, [agent.data, file, draft])

  const graph = useMemo(() => {
    if (!previewAgent) return { nodes: [], edges: [], view: null }
    return buildAgentGraph(previewAgent)
  }, [previewAgent])

  return (
    <>
      {/* Editor column */}
      <section className="flex flex-col border-r border-border overflow-hidden">
        <header className="flex items-center justify-between px-6 py-3 border-b border-border">
          <div className="flex items-center gap-2 text-sm">
            <FileCode className="size-4 text-muted-foreground" />
            <span className="font-mono">{agentName}/{FILE_LABEL[file]}</span>
            {dirty && <Badge variant="approval">unsaved</Badge>}
          </div>
          <Button
            size="sm"
            onClick={() => saveMutation.mutate({ [file]: draft })}
            disabled={!dirty || saveMutation.isPending}
            className="gap-2"
          >
            <Save className="size-3.5" />
            {saveMutation.isPending ? 'Saving…' : 'Save'}
          </Button>
        </header>
        <textarea
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value)
            setDirty(e.target.value !== (agent.data?.[file] ?? ''))
          }}
          spellCheck={false}
          className="flex-1 resize-none bg-background p-6 font-mono text-sm leading-relaxed text-foreground focus:outline-none"
        />
      </section>

      {/* Live graph column */}
      <section className="flex flex-col overflow-hidden">
        <header className="flex items-center justify-between px-6 py-3 border-b border-border">
          <div className="flex items-center gap-2 text-sm">
            <span className="font-medium">Live preview</span>
            <span className="text-muted-foreground text-xs">
              updates as you type
            </span>
          </div>
          <div className="flex items-center gap-1.5 text-xs">
            <Badge variant="allow">allow</Badge>
            <Badge variant="approval">approval</Badge>
            <Badge variant="deny">deny</Badge>
          </div>
        </header>
        <div className="flex-1 relative">
          {graph.view === null ? (
            <div className="absolute inset-0 grid place-items-center gap-2 text-center">
              <AlertTriangle className="size-6 text-approval" />
              <div className="text-sm text-muted-foreground">
                Invalid YAML — fix to render the graph.
              </div>
            </div>
          ) : (
            <ReactFlow
              nodes={graph.nodes}
              edges={graph.edges}
              nodeTypes={nodeTypes}
              nodesDraggable={false}
              nodesConnectable={false}
              edgesFocusable={false}
              fitView
              fitViewOptions={{ padding: 0.25 }}
              proOptions={{ hideAttribution: true }}
            >
              <Background
                variant={BackgroundVariant.Dots}
                gap={20}
                size={1}
                color="hsl(var(--border))"
              />
            </ReactFlow>
          )}
        </div>
      </section>
    </>
  )
}
