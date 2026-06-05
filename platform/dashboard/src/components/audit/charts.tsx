import { useLayoutEffect, useRef, useState } from 'react'
import { Check, CircleDashed, X } from 'lucide-react'
import type { AuditOutcome } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
import { CHART_COLORS, OUT_LABEL } from './chart-tokens'

const C = CHART_COLORS

export interface Counts {
  allow: number
  deny: number
  needs_approval: number
  total: number
}

export interface ChartBucket extends Counts {
  label: Date
  mode: 'hour' | 'day'
}

export interface BreakdownDatum extends Counts {
  key: string
}

const STACK: AuditOutcome[] = ['allow', 'needs_approval', 'deny']

function useMeasure() {
  const ref = useRef<HTMLDivElement>(null)
  const [w, setW] = useState(0)
  useLayoutEffect(() => {
    if (!ref.current) return
    const ro = new ResizeObserver((ents) => setW(ents[0].contentRect.width))
    ro.observe(ref.current)
    setW(ref.current.clientWidth)
    return () => ro.disconnect()
  }, [])
  return [ref, w] as const
}

export function DecisionBadge({ d }: { d: AuditOutcome }) {
  const OutcomeIcon = d === 'allow' ? Check : d === 'deny' ? X : CircleDashed
  return (
    <Badge variant={d === 'needs_approval' ? 'approval' : d}>
      <OutcomeIcon className="size-3" strokeWidth={2} />
      {OUT_LABEL[d]}
    </Badge>
  )
}

// ——— Sparkline (single series area) ———
export function Sparkline({
  data,
  color = C.primary,
  height = 34,
  width = 150,
}: {
  data: number[]
  color?: string
  height?: number
  width?: number
}) {
  if (!data.length) return null
  const max = Math.max(...data, 1)
  const pts = data.map((v, i) => {
    const x = (i / Math.max(data.length - 1, 1)) * width
    const y = height - (v / max) * (height - 3) - 1.5
    return [x, y] as const
  })
  const line = pts.map((p) => p.join(',')).join(' ')
  const area = `0,${height} ${line} ${width},${height}`
  return (
    <svg width={width} height={height} style={{ display: 'block', overflow: 'visible' }}>
      <polygon points={area} fill={color} opacity="0.12" />
      <polyline points={line} fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={pts[pts.length - 1][0]} cy={pts[pts.length - 1][1]} r="2" fill={color} />
    </svg>
  )
}

// ——— Stacked area: decisions over time (allow / approval / deny) ———
export function AreaChart({ days, height = 240 }: { days: ChartBucket[]; height?: number }) {
  const [ref, w] = useMeasure()
  const [hover, setHover] = useState<number | null>(null)
  const padL = 8, padR = 8, padT = 14, padB = 26
  const W = Math.max(w, 320)
  const innerW = W - padL - padR
  const innerH = height - padT - padB
  const maxTotal = Math.max(...days.map((d) => d.total), 1)
  const x = (i: number) => padL + (i / Math.max(days.length - 1, 1)) * innerW
  const y = (v: number) => padT + innerH - (v / maxTotal) * innerH

  const cum = days.map(() => 0)
  const layers: { key: AuditOutcome; path: string }[] = []
  for (const key of STACK) {
    const top = days.map((d, i) => cum[i] + d[key])
    const bottom = cum.slice()
    const path =
      top.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i)},${y(v)}`).join(' ') +
      ' ' +
      bottom.map((_, i) => `L${x(days.length - 1 - i)},${y(bottom[days.length - 1 - i])}`).join(' ') +
      ' Z'
    layers.push({ key, path })
    days.forEach((_, i) => (cum[i] = top[i]))
  }

  const n = days.length
  const mode = (days[0] && days[0].mode) || 'day'
  const ticks = (() => {
    const want = Math.min(5, n)
    if (n <= 1) return [0]
    const step = (n - 1) / (want - 1)
    const s = new Set<number>()
    for (let i = 0; i < want; i++) s.add(Math.round(i * step))
    return [...s].sort((a, b) => a - b)
  })()
  const fmt = (dt: Date) =>
    mode === 'hour'
      ? dt.toLocaleTimeString('en-US', { hour: 'numeric', hour12: true }).replace(' ', '').toLowerCase()
      : dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })

  return (
    <div
      ref={ref}
      style={{ width: '100%', position: 'relative' }}
      onMouseLeave={() => setHover(null)}
      onMouseMove={(e) => {
        const rect = e.currentTarget.getBoundingClientRect()
        const rx = e.clientX - rect.left - padL
        const i = Math.round((rx / innerW) * (days.length - 1))
        setHover(Math.max(0, Math.min(days.length - 1, i)))
      }}
    >
      <svg width={W} height={height} style={{ display: 'block' }}>
        {[0.25, 0.5, 0.75, 1].map((f) => (
          <line key={f} x1={padL} x2={W - padR} y1={padT + innerH * (1 - f)} y2={padT + innerH * (1 - f)} stroke={C.grid} strokeOpacity="0.5" strokeDasharray="2 4" />
        ))}
        {layers.map((l) => (
          <path key={l.key} d={l.path} fill={C[l.key]} opacity={l.key === 'allow' ? 0.16 : l.key === 'needs_approval' ? 0.4 : 0.55} />
        ))}
        {(() => {
          const c2 = days.map(() => 0)
          return STACK.map((key) => {
            const top = days.map((d, i) => c2[i] + d[key])
            const line = top.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i)},${y(v)}`).join(' ')
            days.forEach((_, i) => (c2[i] = top[i]))
            return <path key={key} d={line} fill="none" stroke={C[key]} strokeWidth="1.5" strokeOpacity="0.9" />
          })
        })()}
        {hover != null && (
          <g>
            <line x1={x(hover)} x2={x(hover)} y1={padT} y2={padT + innerH} stroke={C.muted} strokeOpacity="0.5" />
            <circle cx={x(hover)} cy={y(cum[hover])} r="3" fill={C.primary} />
          </g>
        )}
        {ticks.map((t) => (
          <text key={t} x={x(t)} y={height - 8} fontSize="10.5" fill={C.muted} textAnchor={t === 0 ? 'start' : t === n - 1 ? 'end' : 'middle'} fontFamily="var(--font-mono)">
            {fmt(days[t].label)}
          </text>
        ))}
      </svg>
      {hover != null && (
        <div
          style={{
            position: 'absolute', left: Math.min(Math.max(x(hover) + 10, 8), W - 150), top: 8,
            background: 'hsl(var(--popover))', border: '1px solid hsl(var(--border))', borderRadius: 8,
            padding: '8px 10px', fontSize: 12, pointerEvents: 'none', zIndex: 5, minWidth: 130,
            boxShadow: '0 8px 24px hsl(222 40% 2% / 0.5)',
          }}
        >
          <div style={{ color: C.muted, fontSize: 11, marginBottom: 5 }}>
            {mode === 'hour'
              ? days[hover].label.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true })
              : days[hover].label.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })}
          </div>
          {STACK.slice().reverse().map((k) => (
            <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 6, lineHeight: 1.7 }}>
              <span style={{ width: 8, height: 8, borderRadius: 2, background: C[k] }} />
              <span style={{ color: C.muted, flex: 1 }}>{OUT_LABEL[k]}</span>
              <span className="font-mono" style={{ color: 'hsl(var(--foreground))' }}>{days[hover][k]}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ——— Donut: outcome breakdown ———
export function Donut({ counts, size = 168, stroke = 20 }: { counts: Counts; size?: number; stroke?: number }) {
  const r = (size - stroke) / 2
  const cx = size / 2, cy = size / 2
  const circ = 2 * Math.PI * r
  const frac = (k: AuditOutcome) => (counts.total ? counts[k] / counts.total : 0)
  // Cumulative offset via prefix sum (no render-time reassignment).
  const segs = STACK.map((k, i) => ({
    k,
    dash: frac(k) * circ,
    offset: STACK.slice(0, i).reduce((s, kk) => s + frac(kk), 0) * circ,
  }))
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 18 }}>
      <svg width={size} height={size} style={{ flexShrink: 0, transform: 'rotate(-90deg)' }}>
        <circle cx={cx} cy={cy} r={r} fill="none" stroke="hsl(var(--secondary))" strokeWidth={stroke} />
        {segs.map((s) => (
          <circle key={s.k} cx={cx} cy={cy} r={r} fill="none" stroke={C[s.k]} strokeWidth={stroke}
            strokeDasharray={`${s.dash} ${circ - s.dash}`} strokeDashoffset={-s.offset} strokeLinecap="butt" />
        ))}
      </svg>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 26, fontWeight: 600, letterSpacing: '-0.02em', lineHeight: 1 }}>{counts.total.toLocaleString()}</div>
        <div style={{ fontSize: 12, color: C.muted, marginBottom: 12 }}>decisions</div>
        {STACK.map((k) => {
          const pct = counts.total ? Math.round((counts[k] / counts.total) * 100) : 0
          return (
            <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '3px 0', fontSize: 12.5 }}>
              <span style={{ width: 9, height: 9, borderRadius: 2, background: C[k] }} />
              <span style={{ flex: 1, color: 'hsl(var(--foreground))' }}>{OUT_LABEL[k]}</span>
              <span className="font-mono" style={{ color: 'hsl(var(--foreground))' }}>{counts[k].toLocaleString()}</span>
              <span className="font-mono" style={{ color: C.muted, width: 34, textAlign: 'right' }}>{pct}%</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ——— Stacked breakdown bar row (allow/approval/deny) ———
export function BreakdownBar({
  label,
  row,
  max,
  onClick,
  active,
}: {
  label: string
  row: BreakdownDatum
  max: number
  onClick?: () => void
  active?: boolean
}) {
  const seg = (k: AuditOutcome) => (row.total ? (row[k] / row.total) * 100 : 0)
  const widthPct = max ? (row.total / max) * 100 : 0
  return (
    <div
      onClick={onClick}
      style={{ marginBottom: 11, cursor: onClick ? 'pointer' : 'default', opacity: active === false ? 0.45 : 1, transition: 'opacity 120ms' }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 5, fontSize: 12.5, gap: 8 }}>
        <span className="font-mono" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{label}</span>
        <span style={{ color: C.muted, flexShrink: 0 }}>
          {row.deny > 0 && <span style={{ color: C.deny, marginRight: 8 }}>{row.deny} denied</span>}
          <span style={{ color: 'hsl(var(--foreground))' }}>{row.total.toLocaleString()}</span>
        </span>
      </div>
      <div style={{ display: 'flex', height: 6, borderRadius: 3, overflow: 'hidden', background: 'hsl(var(--secondary))', width: `${Math.max(widthPct, 4)}%`, minWidth: 24 }}>
        <div style={{ width: `${seg('allow')}%`, background: C.allow }} />
        <div style={{ width: `${seg('needs_approval')}%`, background: C.needs_approval }} />
        <div style={{ width: `${seg('deny')}%`, background: C.deny }} />
      </div>
    </div>
  )
}
