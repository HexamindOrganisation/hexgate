import { useEffect, useMemo, useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { api, type AuditDecisionRow, type AuditOutcome } from '@/lib/api'
import { useProjectScoped } from '@/lib/active'
import { NoProjectEmptyState } from '@/components/NoProjectEmptyState'
import { Icon } from '@/components/audit/icon'
import {
  AreaChart,
  type ChartBucket,
  type Counts,
  DecisionBadge,
  Donut,
} from '@/components/audit/charts'
import { CHART_COLORS, OUT_LABEL } from '@/components/audit/chart-tokens'
import {
  ActiveChips,
  BreakdownCard,
  EventsTable,
  type Filters,
  FilterBar,
  KpiCard,
} from '@/components/audit/pieces'
import '@/styles/audit.css'

const ZERO_COUNTS: Counts = { allow: 0, deny: 0, needs_approval: 0, total: 0 }

const fmtTs = (d: Date) =>
  d.toLocaleString('en-US', {
    month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }) + '.' + String(d.getMilliseconds()).padStart(3, '0')
const fmtFull = (d: Date) => d.toISOString().replace('T', ' ').replace('Z', ' UTC')

// ————————————————————————————————————————————— Detail drawer
function KV({ k, children, mono, muted }: { k: string; children: React.ReactNode; mono?: boolean; muted?: boolean }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '116px 1fr', gap: 10, padding: '5px 0', fontSize: 12.5, alignItems: 'baseline' }}>
      <div style={{ color: 'hsl(var(--muted-foreground))' }}>{k}</div>
      <div className={mono ? 'mono' : ''} style={{ color: muted ? 'hsl(var(--muted-foreground))' : 'hsl(var(--foreground))', wordBreak: 'break-word', fontSize: mono ? 12 : 12.5 }}>{children}</div>
    </div>
  )
}
function DrawerSection({ label, children, accent }: { label: string; children: React.ReactNode; accent?: string }) {
  return (
    <div style={{ marginTop: 22 }}>
      <div style={{ fontSize: 11, color: accent || 'hsl(var(--muted-foreground))', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 8, fontWeight: 500 }}>{label}</div>
      {children}
    </div>
  )
}

function DetailDrawer({
  event, related, onClose, onSelect, setF,
}: {
  event: AuditDecisionRow | null
  related: AuditDecisionRow[]
  onClose: () => void
  onSelect: (e: AuditDecisionRow) => void
  setF: (u: (p: Filters) => Filters) => void
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  if (!event) return null
  const e = event
  const argStr = e.arguments == null ? '—' : JSON.stringify(e.arguments, null, 2)
  const hintStr = typeof e.hint === 'string' ? e.hint : e.hint == null ? '' : JSON.stringify(e.hint)
  const accent = e.outcome === 'deny' ? 'hsl(var(--semantic-deny))' : e.outcome === 'needs_approval' ? 'hsl(var(--semantic-approval))' : 'hsl(var(--semantic-allow))'

  return (
    <div style={{ position: 'fixed', inset: 0, zIndex: 50, display: 'flex', justifyContent: 'flex-end' }}>
      <div onClick={onClose} style={{ position: 'absolute', inset: 0, background: 'hsl(222 40% 2% / 0.55)', backdropFilter: 'blur(1px)' }} />
      <aside style={{ position: 'relative', width: 472, maxWidth: '92vw', background: 'hsl(var(--card))', borderLeft: '1px solid hsl(var(--border))', height: '100%', display: 'flex', flexDirection: 'column', boxShadow: '-24px 0 60px hsl(222 40% 2% / 0.5)' }}>
        <div style={{ padding: '16px 20px', borderBottom: '1px solid hsl(var(--border))', display: 'flex', alignItems: 'center', gap: 10 }}>
          <DecisionBadge d={e.outcome} />
          <span className="mono" style={{ fontSize: 12, color: 'hsl(var(--muted-foreground))', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>{e.event_id}</span>
          <button className="fty-iconbtn" onClick={onClose} title="Close (Esc)"><Icon name="x" size={16} /></button>
        </div>

        <div style={{ overflow: 'auto', flex: 1, padding: '4px 20px 24px' }}>
          <div style={{ marginTop: 18, display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
            <span className="mono" style={{ fontSize: 17, fontWeight: 600, color: 'hsl(var(--foreground))' }}>{e.tool_name}</span>
            <span style={{ fontSize: 13, color: 'hsl(var(--muted-foreground))' }}>by</span>
            <span className="mono" style={{ fontSize: 14, color: 'hsl(var(--foreground))' }}>{e.agent_name}</span>
          </div>
          <div style={{ marginTop: 8, fontSize: 13, color: 'hsl(var(--foreground))', lineHeight: 1.55, padding: '10px 12px', background: `color-mix(in srgb, ${accent} 9%, transparent)`, border: `1px solid color-mix(in srgb, ${accent} 28%, transparent)`, borderRadius: 8 }}>
            {e.reason || (e.outcome === 'allow' ? 'Allowed — no rule violated.' : 'No reason recorded.')}
            {e.error_type && <span className="mono" style={{ display: 'block', marginTop: 6, fontSize: 11.5, color: accent }}>error_type: {e.error_type}</span>}
          </div>

          {e.violations.length > 0 && (
            <DrawerSection label="Violations" accent="hsl(var(--semantic-deny))">
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                {e.violations.map((v) => (
                  <span key={v} className="mono" style={{ fontSize: 11.5, padding: '3px 8px', borderRadius: 6, background: 'hsl(var(--semantic-deny-soft))', color: 'hsl(var(--semantic-deny))' }}>{v}</span>
                ))}
              </div>
            </DrawerSection>
          )}

          {hintStr && (
            <DrawerSection label="Policy hint">
              <div className="fty-code" style={{ fontSize: 12 }}>
                <Icon name="lightbulb" size={13} color="hsl(var(--semantic-approval))" />
                <span style={{ color: 'hsl(var(--foreground))' }}>{hintStr}</span>
              </div>
            </DrawerSection>
          )}

          <DrawerSection label="Decision">
            <KV k="outcome">{OUT_LABEL[e.outcome]}</KV>
            <KV k="tool_name" mono>{e.tool_name}</KV>
            <KV k="role">{e.role || <span style={{ color: 'hsl(var(--muted-foreground))' }}>∅ none</span>}</KV>
            {e.error_type && <KV k="error_type" mono>{e.error_type}</KV>}
          </DrawerSection>

          <DrawerSection label="Arguments">
            <pre style={{ margin: 0, padding: 12, background: 'hsl(var(--muted))', border: '1px solid hsl(var(--border))', borderRadius: 8, fontSize: 11.5, fontFamily: 'var(--font-mono)', lineHeight: 1.6, whiteSpace: 'pre-wrap', color: 'hsl(var(--foreground))' }}>{argStr}</pre>
          </DrawerSection>

          <DrawerSection label="Envelope">
            <KV k="occurred_at" mono>{fmtFull(new Date(e.occurred_at))}</KV>
            <KV k="received_at" mono muted>{fmtFull(new Date(e.received_at))}</KV>
            <KV k="agent_name" mono>{e.agent_name}</KV>
            <KV k="session_id" mono muted>{e.session_id || '∅'}</KV>
            <KV k="user_id" mono muted>{e.user_id || '∅'}</KV>
          </DrawerSection>

          <DrawerSection label={`Same session · ${related.length}`}>
            {related.length ? (
              <div style={{ border: '1px solid hsl(var(--border))', borderRadius: 8, overflow: 'hidden' }}>
                {related.map((r, i) => (
                  <div key={r.event_id} onClick={() => onSelect(r)}
                    style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 10px', cursor: 'pointer', borderBottom: i < related.length - 1 ? '1px solid hsl(var(--border))' : 0, fontSize: 12 }}>
                    <DecisionBadge d={r.outcome} />
                    <span className="mono" style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.tool_name}</span>
                    <span className="mono" style={{ color: 'hsl(var(--muted-foreground))', fontSize: 11 }}>{fmtTs(new Date(r.occurred_at))}</span>
                  </div>
                ))}
              </div>
            ) : <div style={{ fontSize: 12, color: 'hsl(var(--muted-foreground))' }}>No other events in this session.</div>}
          </DrawerSection>
        </div>

        <div style={{ padding: '12px 20px', borderTop: '1px solid hsl(var(--border))', display: 'flex', gap: 8 }}>
          <button className="fty-btn secondary sm" onClick={() => { setF((p) => ({ ...p, agent: e.agent_name })); onClose() }}><Icon name="filter" size={12} />Filter to agent</button>
          <button className="fty-btn secondary sm" onClick={() => { setF((p) => ({ ...p, tool: e.tool_name })); onClose() }}><Icon name="filter" size={12} />Filter to tool</button>
        </div>
      </aside>
    </div>
  )
}

// ————————————————————————————————————————————— Root
const EMPTY_FILTERS: Filters = { agent: '', role: '', tool: '', outcome: '', range: '30d' }

export function AuditPage() {
  const projectScope = useProjectScoped()
  const projectId = projectScope.projectId
  const [f, setFState] = useState<Filters>(EMPTY_FILTERS)
  const [sel, setSel] = useState<AuditDecisionRow | null>(null)
  const [tableLimit, setTableLimit] = useState(40)

  // Any filter change resets the table page (wrapping the setter avoids a
  // setState-in-effect).
  const setF: (u: (p: Filters) => Filters) => void = (updater) => {
    setFState(updater)
    setTableLimit(40)
  }

  const scope = { window: f.range, agent: f.agent, role: f.role, tool: f.tool }

  // Range-only (unscoped) summary: filter dropdown options + the "X of Y" total.
  const optionsQ = useQuery({
    queryKey: ['audit', 'options', projectId, f.range],
    enabled: !!projectId,
    queryFn: () => api.getAuditSummary({ window: f.range }, projectId as string),
  })
  // Scoped summary/timeseries: KPIs, donut, area, breakdown.
  const summaryQ = useQuery({
    queryKey: ['audit', 'summary', projectId, scope],
    enabled: !!projectId,
    queryFn: () => api.getAuditSummary(scope, projectId as string),
    placeholderData: keepPreviousData,
    refetchInterval: 30_000,
  })
  const tsQ = useQuery({
    queryKey: ['audit', 'ts', projectId, scope],
    enabled: !!projectId,
    queryFn: () => api.getAuditTimeseries(scope, projectId as string),
    placeholderData: keepPreviousData,
    refetchInterval: 30_000,
  })
  const listFilters = { ...scope, outcome: f.outcome || undefined, limit: tableLimit }
  const listQ = useQuery({
    queryKey: ['audit', 'list', projectId, listFilters],
    enabled: !!projectId,
    queryFn: () => api.listAuditDecisions(listFilters, projectId as string),
    placeholderData: keepPreviousData,
    refetchInterval: 30_000,
  })
  const relatedQ = useQuery({
    queryKey: ['audit', 'session', projectId, f.range, sel?.session_id],
    enabled: !!sel?.session_id && !!projectId,
    queryFn: () =>
      api.listAuditDecisions(
        { window: f.range, session_id: sel!.session_id, limit: 12 },
        projectId as string,
      ),
  })

  const summary = summaryQ.data
  const counts: Counts = summary
    ? { allow: summary.totals.allow, deny: summary.totals.deny, needs_approval: summary.totals.needs_approval, total: summary.totals.all }
    : ZERO_COUNTS

  const days: ChartBucket[] = useMemo(
    () => (tsQ.data ?? []).map((p) => ({
      label: new Date(p.bucket),
      mode: f.range === '24h' ? 'hour' : 'day',
      allow: p.allow, deny: p.deny, needs_approval: p.needs_approval,
      total: p.allow + p.deny + p.needs_approval,
    })),
    [tsQ.data, f.range],
  )
  const spark = useMemo(() => ({
    total: days.map((d) => d.total),
    allow: days.map((d) => d.allow),
    deny: days.map((d) => d.deny),
    needs_approval: days.map((d) => d.needs_approval),
  }), [days])

  const denyRate = counts.total ? ((counts.deny / counts.total) * 100).toFixed(1) : '0.0'
  const allowPct = counts.total ? Math.round((counts.allow / counts.total) * 100) : 0
  const apprPct = counts.total ? Math.round((counts.needs_approval / counts.total) * 100) : 0
  const nDays = f.range === '7d' ? 7 : f.range === '90d' ? 90 : 30
  const avgLabel = f.range === '24h' ? `${(counts.total / 24).toFixed(1)}/hr avg` : `${(counts.total / nDays).toFixed(0)}/day avg`
  const setOutcome = (o: AuditOutcome) => setF((p) => ({ ...p, outcome: p.outcome === o ? '' : o }))

  const options = optionsQ.data
  const rangeTotal = options?.totals.all ?? 0
  const related = (relatedQ.data?.rows ?? []).filter((r) => r.event_id !== sel?.event_id).slice(0, 6)

  const exportJsonl = () => {
    const rows = listQ.data?.rows ?? []
    if (!rows.length) return
    const blob = new Blob([rows.map((r) => JSON.stringify(r)).join('\n')], { type: 'application/x-ndjson' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'decisions.jsonl'
    a.click()
    URL.revokeObjectURL(url)
  }

  if (projectScope.status === 'no-project') {
    return <NoProjectEmptyState resource="audit events" />
  }

  return (
    <>
      <div className="fty-page">
        <div className="fty-page-hd">
          <div>
            <h1 className="fty-page-title">Audit</h1>
            <div className="fty-page-sub">Every policy decision for project <span className="mono" style={{ color: 'hsl(var(--foreground))' }}>support-bot</span></div>
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <div className="fty-seg" style={{ width: 'auto' }}>
              {(['24h', '7d', '30d', '90d'] as const).map((r) => (
                <button key={r} className={f.range === r ? 'active' : ''} onClick={() => setF((p) => ({ ...p, range: r }))}>{r}</button>
              ))}
            </div>
          </div>
        </div>

        <FilterBar
          f={f}
          setF={setF}
          shown={listQ.data?.total ?? 0}
          total={rangeTotal}
          agents={options?.by_agent.map((r) => r.key) ?? []}
          roles={options?.by_role.map((r) => r.key) ?? []}
          tools={options?.by_tool.map((r) => r.key) ?? []}
        />
        <ActiveChips f={f} setF={setF} />

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 16, marginBottom: 16 }}>
          <KpiCard label="Decisions" icon="activity" value={counts.total.toLocaleString()} sub={avgLabel} spark={spark.total} sparkColor="hsl(var(--primary))" showSpark />
          <KpiCard label="Allowed" icon="check" value={counts.allow.toLocaleString()} color="allow" sub={`${allowPct}% of decisions`} spark={spark.allow} sparkColor="hsl(var(--semantic-allow))" showSpark onClick={() => setOutcome('allow')} active={f.outcome === 'allow'} />
          <KpiCard label="Denied" icon="x" value={counts.deny.toLocaleString()} color="deny" sub={`${denyRate}% deny rate`} spark={spark.deny} sparkColor="hsl(var(--semantic-deny))" showSpark onClick={() => setOutcome('deny')} active={f.outcome === 'deny'} />
          <KpiCard label="Needs approval" icon="circle-dashed" value={counts.needs_approval.toLocaleString()} color="approval" sub={`${apprPct}% of decisions`} spark={spark.needs_approval} sparkColor="hsl(var(--semantic-approval))" showSpark onClick={() => setOutcome('needs_approval')} active={f.outcome === 'needs_approval'} />
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: '1.55fr 1fr', gap: 16, marginBottom: 16 }}>
          <div className="fty-card pad-lg">
            <div className="fty-card-hd">
              <div className="fty-card-title">Decisions over time</div>
              <div style={{ display: 'flex', gap: 14, fontSize: 11, color: 'hsl(var(--muted-foreground))' }}>
                {(['allow', 'needs_approval', 'deny'] as AuditOutcome[]).map((k) => (
                  <span key={k} style={{ display: 'flex', alignItems: 'center', gap: 5 }}><span style={{ width: 8, height: 8, borderRadius: 2, background: CHART_COLORS[k] }} />{OUT_LABEL[k]}</span>
                ))}
              </div>
            </div>
            {days.length ? <AreaChart days={days} /> : <div style={{ height: 240, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 13, color: 'hsl(var(--muted-foreground))' }}>No decisions in this range.</div>}
          </div>
          <div className="fty-card pad-lg">
            <div className="fty-card-title" style={{ marginBottom: 18 }}>Outcome breakdown</div>
            <Donut counts={counts} />
          </div>
        </div>

        <div style={{ marginBottom: 16 }}>
          <BreakdownCard byTool={summary?.by_tool ?? []} byAgent={summary?.by_agent ?? []} byRole={summary?.by_role ?? []} f={f} setF={setF} />
        </div>

        <EventsTable
          rows={listQ.data?.rows ?? []}
          total={listQ.data?.total ?? 0}
          onSelect={setSel}
          selectedId={sel?.event_id}
          onLoadMore={() => setTableLimit((l) => Math.min(l + 40, 200))}
          loadingMore={listQ.isFetching}
          onExport={exportJsonl}
        />
      </div>

      <DetailDrawer event={sel} related={related} onClose={() => setSel(null)} onSelect={setSel} setF={setF} />
    </>
  )
}
