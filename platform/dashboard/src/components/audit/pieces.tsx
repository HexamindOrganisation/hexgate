import { useMemo, useState } from 'react'
import type {
  AuditBreakdownRow,
  AuditDecisionRow,
  AuditOutcome,
} from '@/lib/api'
import { Icon } from './icon'
import { BreakdownBar, type BreakdownDatum, DecisionBadge, Sparkline } from './charts'
import { CHART_COLORS, OUT_LABEL } from './chart-tokens'

// Shared filter state. '' = "all"; outcome applies to the table only.
export interface Filters {
  agent: string
  role: string
  tool: string
  outcome: '' | AuditOutcome
  range: '24h' | '7d' | '30d' | '90d'
}

export type SetFilters = (updater: (prev: Filters) => Filters) => void

const fmtTs = (d: Date) =>
  d.toLocaleString('en-US', {
    month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }) + '.' + String(d.getMilliseconds()).padStart(3, '0')

// Map a server breakdown row ({key, all, ...}) to the chart datum ({total, ...}).
const toDatum = (r: AuditBreakdownRow): BreakdownDatum => ({
  key: r.key,
  total: r.all,
  allow: r.allow,
  deny: r.deny,
  needs_approval: r.needs_approval,
})

// Module-level (not re-created per render).
function FilterSelect({ value, all, opts, onChange }: { value: string; all: string; opts: string[]; onChange: (v: string) => void }) {
  return (
    <select className="fty-select" value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">{all}</option>
      {opts.map((o) => <option key={o} value={o}>{o}</option>)}
    </select>
  )
}

// ————————————————————————————————————————————— Filter bar
export function FilterBar({
  f, setF, shown, total, agents, roles, tools,
}: {
  f: Filters
  setF: SetFilters
  shown: number
  total: number
  agents: string[]
  roles: string[]
  tools: string[]
}) {
  const set = <K extends keyof Filters>(k: K, v: Filters[K]) => setF((p) => ({ ...p, [k]: v }))
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginBottom: 14 }}>
      <FilterSelect value={f.agent} all="All agents" opts={agents} onChange={(v) => set('agent', v)} />
      <FilterSelect value={f.role} all="All roles" opts={roles} onChange={(v) => set('role', v)} />
      <FilterSelect value={f.tool} all="All tools" opts={tools} onChange={(v) => set('tool', v)} />
      <span style={{ fontSize: 12, color: 'hsl(var(--muted-foreground))', marginLeft: 4 }}>Outcome</span>
      <div className="fty-seg" style={{ width: 'auto' }}>
        <button className={f.outcome === '' ? 'active' : ''} onClick={() => set('outcome', '')}><Icon name="list" size={12} />All</button>
        <button className={f.outcome === 'allow' ? 'active allow' : ''} onClick={() => set('outcome', 'allow')} style={{ color: f.outcome === 'allow' ? undefined : 'hsl(var(--semantic-allow))' }}><Icon name="check" size={12} strokeWidth={2} />allow</button>
        <button className={f.outcome === 'deny' ? 'active deny' : ''} onClick={() => set('outcome', 'deny')} style={{ color: f.outcome === 'deny' ? undefined : 'hsl(var(--semantic-deny))' }}><Icon name="x" size={12} strokeWidth={2} />deny</button>
        <button className={f.outcome === 'needs_approval' ? 'active approval' : ''} onClick={() => set('outcome', 'needs_approval')} style={{ color: f.outcome === 'needs_approval' ? undefined : 'hsl(var(--semantic-approval))' }}><Icon name="circle-dashed" size={12} strokeWidth={2} />approval</button>
      </div>
      <span style={{ fontSize: 12, color: 'hsl(var(--muted-foreground))', marginLeft: 'auto', whiteSpace: 'nowrap' }}>
        <span style={{ color: 'hsl(var(--foreground))' }}>{shown.toLocaleString()}</span> of <span className="mono">{total.toLocaleString()}</span> decisions
      </span>
    </div>
  )
}

export function ActiveChips({ f, setF }: { f: Filters; setF: SetFilters }) {
  const set = <K extends keyof Filters>(k: K, v: Filters[K]) => setF((p) => ({ ...p, [k]: v }))
  const lbl: Record<string, string> = { agent: 'agent', role: 'role', tool: 'tool', outcome: 'outcome' }
  const chips = (['agent', 'role', 'tool', 'outcome'] as const).filter((k) => f[k])
  if (!chips.length) return null
  return (
    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 16, alignItems: 'center' }}>
      <span style={{ fontSize: 11.5, color: 'hsl(var(--muted-foreground))' }}>Filters</span>
      {chips.map((k) => (
        <span key={k} className="fty-badge muted" style={{ paddingRight: 4 }}>
          {lbl[k]}: <span className="mono" style={{ color: 'hsl(var(--foreground))' }}>{f[k]}</span>
          <button onClick={() => set(k, '')} style={{ border: 0, background: 'transparent', cursor: 'pointer', color: 'hsl(var(--muted-foreground))', display: 'inline-flex', padding: 0, marginLeft: 2 }}><Icon name="x" size={11} /></button>
        </span>
      ))}
      <button className="fty-btn ghost sm" onClick={() => setF((p) => ({ agent: '', role: '', tool: '', outcome: '', range: p.range }))}>Clear all</button>
    </div>
  )
}

// ————————————————————————————————————————————— KPI tile
export function KpiCard({
  label, icon, value, sub, color, spark, sparkColor, showSpark, onClick, active,
}: {
  label: string
  icon: string
  value: string
  sub: string
  color?: 'allow' | 'deny' | 'approval'
  spark?: number[]
  sparkColor?: string
  showSpark?: boolean
  onClick?: () => void
  active?: boolean
}) {
  return (
    <div className="fty-card" onClick={onClick}
      style={{ cursor: onClick ? 'pointer' : 'default', borderColor: active ? 'hsl(var(--primary) / 0.5)' : undefined, transition: 'border-color 120ms' }}>
      <div className="fty-kpi">
        <div className="fty-kpi-label"><Icon name={icon} size={13} /><span>{label}</span></div>
        <div className={`fty-kpi-val ${color || ''}`}>{value}</div>
        <div className="fty-kpi-sub">{sub}</div>
        {showSpark && spark && <div className="fty-kpi-spark"><Sparkline data={spark} color={sparkColor} width={170} /></div>}
      </div>
    </div>
  )
}

// ————————————————————————————————————————————— Breakdown card
const DIMS = [
  { id: 'tool' as const, label: 'Tools', fkey: 'tool' as const },
  { id: 'agent' as const, label: 'Agents', fkey: 'agent' as const },
  { id: 'role' as const, label: 'Roles', fkey: 'role' as const },
]

export function BreakdownCard({
  byTool, byAgent, byRole, f, setF,
}: {
  byTool: AuditBreakdownRow[]
  byAgent: AuditBreakdownRow[]
  byRole: AuditBreakdownRow[]
  f: Filters
  setF: SetFilters
}) {
  const [dim, setDim] = useState<'tool' | 'agent' | 'role'>('tool')
  const [sort, setSort] = useState<'volume' | 'denials'>('volume')
  const source = dim === 'tool' ? byTool : dim === 'agent' ? byAgent : byRole
  const data = useMemo(() => {
    let d = source.map(toDatum)
    if (sort === 'denials') d = d.slice().sort((a, b) => b.deny - a.deny || b.total - a.total)
    return d.slice(0, 10)
  }, [source, sort])
  const max = Math.max(...data.map((d) => d.total), 1)
  const fkey = dim

  return (
    <div className="fty-card pad-lg" style={{ display: 'flex', flexDirection: 'column' }}>
      <div className="fty-card-hd" style={{ marginBottom: 16 }}>
        <div className="fty-tabs">
          {DIMS.map((d) => (
            <button key={d.id} className={dim === d.id ? 'active' : ''} onClick={() => setDim(d.id)}>{d.label}</button>
          ))}
        </div>
        <select className="fty-select" value={sort} onChange={(e) => setSort(e.target.value as 'volume' | 'denials')} style={{ height: 28, fontSize: 12 }}>
          <option value="volume">by volume</option>
          <option value="denials">by denials</option>
        </select>
      </div>
      <div style={{ flex: 1, display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', columnGap: 32, rowGap: 0 }}>
        {data.map((row) => (
          <BreakdownBar key={row.key} label={row.key} row={row} max={max}
            active={!f[fkey] || f[fkey] === row.key}
            onClick={() => setF((p) => ({ ...p, [fkey]: p[fkey] === row.key ? '' : row.key }))} />
        ))}
        {!data.length && <div style={{ fontSize: 12.5, color: 'hsl(var(--muted-foreground))', padding: '8px 0' }}>No decisions match.</div>}
      </div>
      <div style={{ display: 'flex', gap: 14, marginTop: 4, paddingTop: 12, borderTop: '1px solid hsl(var(--border))', fontSize: 11, color: 'hsl(var(--muted-foreground))' }}>
        {(['allow', 'needs_approval', 'deny'] as AuditOutcome[]).map((k) => (
          <span key={k} style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <span style={{ width: 8, height: 8, borderRadius: 2, background: CHART_COLORS[k] }} />{OUT_LABEL[k]}
          </span>
        ))}
        <span style={{ marginLeft: 'auto' }}>click a bar to filter →</span>
      </div>
    </div>
  )
}

// ————————————————————————————————————————————— Events table
export function EventsTable({
  rows, total, density, onSelect, selectedId, onLoadMore, loadingMore, onExport,
}: {
  rows: AuditDecisionRow[]
  total: number
  density?: 'comfortable' | 'compact'
  onSelect: (e: AuditDecisionRow) => void
  selectedId?: string | null
  onLoadMore: () => void
  loadingMore?: boolean
  onExport?: () => void
}) {
  return (
    <div className="fty-card" style={{ padding: 0, overflow: 'hidden' }}>
      <div style={{ padding: '16px 20px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', borderBottom: '1px solid hsl(var(--border))' }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 600 }}>Decisions</div>
          <div style={{ fontSize: 12, color: 'hsl(var(--muted-foreground))', marginTop: 2 }}>Newest first · ordered by <span className="mono">occurred_at</span>. Click a row to inspect.</div>
        </div>
        <button className="fty-btn ghost" onClick={onExport} disabled={!onExport}><Icon name="download" size={13} />Export JSONL</button>
      </div>
      <div style={{ overflowX: 'auto' }}>
        <table className={`fty-table ${density === 'compact' ? 'compact' : ''}`}>
          <thead>
            <tr>
              <th style={{ width: 168 }}>Time</th>
              <th>Agent</th>
              <th>Role</th>
              <th>Tool</th>
              <th style={{ width: 110 }}>Outcome</th>
              <th>Reason</th>
              <th style={{ width: 30 }}></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((e) => (
              <tr key={e.event_id} onClick={() => onSelect(e)}
                style={{ cursor: 'pointer', background: selectedId === e.event_id ? 'hsl(var(--primary) / 0.08)' : undefined }}>
                <td className="mono" style={{ color: 'hsl(var(--muted-foreground))', fontSize: 12 }}>{fmtTs(new Date(e.occurred_at))}</td>
                <td className="mono" style={{ fontSize: 12.5 }}>{e.agent_name}</td>
                <td style={{ color: e.role ? undefined : 'hsl(var(--muted-foreground))' }}>{e.role || '—'}</td>
                <td className="mono" style={{ fontSize: 12.5 }}>{e.tool_name}</td>
                <td><DecisionBadge d={e.outcome} /></td>
                <td style={{ color: 'hsl(var(--muted-foreground))', fontSize: 12.5, maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{e.reason || '—'}</td>
                <td><Icon name="chevron-right" size={14} color="hsl(var(--muted-foreground))" /></td>
              </tr>
            ))}
            {!rows.length && (
              <tr><td colSpan={7} style={{ textAlign: 'center', color: 'hsl(var(--muted-foreground))', padding: '32px 0' }}>No decisions match the current filters.</td></tr>
            )}
          </tbody>
        </table>
      </div>
      {rows.length < total && (
        <div style={{ padding: '12px 20px', borderTop: '1px solid hsl(var(--border))', textAlign: 'center' }}>
          <button className="fty-btn secondary sm" onClick={onLoadMore} disabled={loadingMore}>
            {loadingMore ? 'Loading…' : `Load 40 more · ${(total - rows.length).toLocaleString()} remaining`}
          </button>
        </div>
      )}
    </div>
  )
}
