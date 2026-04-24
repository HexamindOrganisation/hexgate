import { Handle, Position, type NodeProps } from '@xyflow/react'
import { Users, Bot, Wrench } from 'lucide-react'
import { cn } from '@/lib/utils'

interface RoleData {
  label: string
  muted?: boolean
  [key: string]: unknown
}

interface AgentData {
  name: string
  model?: string
  toolCount?: number
  [key: string]: unknown
}

interface ToolData {
  name: string
  mode?: 'allow' | 'deny' | 'approval_required' | 'default'
  [key: string]: unknown
}

const MODE_STRIP: Record<string, string> = {
  allow: 'bg-allow',
  deny: 'bg-deny',
  approval_required: 'bg-approval',
  default: 'bg-muted-foreground',
}

export function RoleNode({ data, selected }: NodeProps) {
  const d = data as RoleData
  return (
    <div
      className={cn(
        'relative flex items-center gap-2.5 rounded-md border px-3 py-2 min-w-[160px] transition-colors',
        selected
          ? 'border-primary bg-primary/10'
          : d.muted
            ? 'border-border bg-card text-muted-foreground'
            : 'border-border bg-card',
      )}
    >
      <Users className={cn('size-4', d.muted ? 'text-muted-foreground' : 'text-primary')} />
      <div>
        <div className="text-sm font-medium text-foreground">{d.label}</div>
        {d.muted && <div className="text-[10px] text-muted-foreground">default</div>}
      </div>
      <Handle type="source" position={Position.Right} className="!bg-border !border-0" />
    </div>
  )
}

export function AgentNode({ data, selected }: NodeProps) {
  const d = data as AgentData
  return (
    <div
      className={cn(
        'relative flex items-start gap-2.5 rounded-md border px-3 py-2 min-w-[180px] transition-colors',
        selected ? 'border-primary bg-primary/10' : 'border-border bg-card',
      )}
    >
      <Bot className="size-4 mt-0.5 text-foreground" />
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-foreground truncate">{d.name}</div>
        <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
          {d.model && <span className="font-mono">{d.model}</span>}
          {typeof d.toolCount === 'number' && (
            <>
              <span>·</span>
              <span>
                {d.toolCount} tool{d.toolCount === 1 ? '' : 's'}
              </span>
            </>
          )}
        </div>
      </div>
      <Handle type="target" position={Position.Left} className="!bg-border !border-0" />
      <Handle type="source" position={Position.Right} className="!bg-border !border-0" />
    </div>
  )
}

export function ToolNode({ data, selected }: NodeProps) {
  const d = data as ToolData
  return (
    <div
      className={cn(
        'relative flex items-center gap-2 rounded-md border px-3 py-2 min-w-[160px] transition-colors overflow-hidden',
        selected ? 'border-primary bg-primary/10' : 'border-border bg-card',
      )}
    >
      <span
        className={cn(
          'absolute left-0 top-0 bottom-0 w-[3px]',
          MODE_STRIP[d.mode ?? 'default'],
        )}
      />
      <Wrench className="size-4 text-muted-foreground ml-1.5" />
      <div className="text-sm font-medium text-foreground font-mono">{d.name}</div>
      <Handle type="target" position={Position.Left} className="!bg-border !border-0" />
    </div>
  )
}

export const nodeTypes = {
  role: RoleNode,
  agent: AgentNode,
  tool: ToolNode,
}
