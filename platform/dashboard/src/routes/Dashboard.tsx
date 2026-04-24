export function DashboardPage() {
  return (
    <div className="max-w-[1400px] mx-auto">
      <h1 className="text-2xl font-semibold tracking-tight">Overview</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        Live view of policy decisions, agents, and the active bundle for{' '}
        <span className="font-mono text-foreground">support-bot</span>.
      </p>
      <div className="mt-8 grid grid-cols-4 gap-4">
        {['Allowed · 24h', 'Denied · 24h', 'Approval queue', 'Active agents'].map((label) => (
          <div
            key={label}
            className="rounded-lg border border-border bg-card p-5"
          >
            <div className="text-xs text-muted-foreground">{label}</div>
            <div className="mt-2 text-3xl font-semibold tabular-nums">—</div>
          </div>
        ))}
      </div>
    </div>
  )
}
