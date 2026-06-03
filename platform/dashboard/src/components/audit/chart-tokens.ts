import type { AuditOutcome } from '@/lib/api'

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
