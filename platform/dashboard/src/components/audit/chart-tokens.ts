import type { AuditOutcome } from '@/lib/api'
import type { ChartSeries } from '@/components/ui/charts'

// Shared palette (--semantic-* tokens). Separate module so charts.tsx exports
// only components (react-refresh).
export const CHART_COLORS = {
  allow: 'hsl(var(--semantic-allow))',
  deny: 'hsl(var(--semantic-deny))',
  needs_approval: 'hsl(var(--semantic-approval))',
  primary: 'hsl(var(--primary))',
  grid: 'hsl(var(--border))',
  muted: 'hsl(var(--muted-foreground))',
} as const

export const OUT_LABEL: Record<AuditOutcome, string> = {
  allow: 'allow',
  deny: 'deny',
  needs_approval: 'approval',
}

// Legend-swatch background classes. Static map — Tailwind's scanner can't
// see interpolated class names like `bg-${k}`.
export const OUT_SWATCH: Record<AuditOutcome, string> = {
  allow: 'bg-allow',
  deny: 'bg-deny',
  needs_approval: 'bg-approval',
}

// Display label for the empty-role bucket. Local to the dashboard — the
// wire carries the raw "" key (and `role=` filters it), so the stored data
// and API never reserve this string. A role literally named "(none)" would
// be display-ambiguous here, but its data stays intact and queryable.
export const NO_VALUE_LABEL = '(none)'

// The outcome stack, in render order, for the generic ui/charts primitives.
export const OUTCOME_SERIES: ChartSeries[] = [
  { key: 'allow', label: OUT_LABEL.allow, color: CHART_COLORS.allow, swatchClass: OUT_SWATCH.allow, fillOpacity: 0.16 },
  { key: 'needs_approval', label: OUT_LABEL.needs_approval, color: CHART_COLORS.needs_approval, swatchClass: OUT_SWATCH.needs_approval, fillOpacity: 0.4 },
  { key: 'deny', label: OUT_LABEL.deny, color: CHART_COLORS.deny, swatchClass: OUT_SWATCH.deny, fillOpacity: 0.55 },
]
