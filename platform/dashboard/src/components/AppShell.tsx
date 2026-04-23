import { NavLink, Outlet } from 'react-router-dom'
import {
  LayoutDashboard,
  Network,
  MessageSquareCode,
  ScrollText,
  KeyRound,
  Settings2,
  Server,
  Fingerprint,
  Shield,
} from 'lucide-react'
import { cn } from '@/lib/utils'

const workspaceLinks = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/graph', label: 'Resource map', icon: Network },
  { to: '/playground', label: 'Playground', icon: MessageSquareCode },
  { to: '/audit', label: 'Audit', icon: ScrollText, badge: '24h' },
  { to: '/tokens', label: 'Tokens', icon: KeyRound },
  { to: '/settings', label: 'Settings', icon: Settings2 },
]

const environmentLinks = [
  { to: '/control-plane', label: 'Control plane', icon: Server, status: 'OK' },
  { to: '/signing-key', label: 'Signing key', icon: Fingerprint },
]

function NavItem({
  to,
  label,
  icon: Icon,
  end,
  badge,
  status,
}: {
  to: string
  label: string
  icon: typeof LayoutDashboard
  end?: boolean
  badge?: string
  status?: string
}) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        cn(
          'flex h-9 items-center justify-between rounded-md px-3 text-sm transition-colors',
          isActive
            ? 'bg-primary/15 text-primary font-medium'
            : 'text-muted-foreground hover:bg-accent hover:text-foreground',
        )
      }
    >
      <span className="flex items-center gap-2.5">
        <Icon className="size-4" />
        {label}
      </span>
      {badge && <span className="text-[11px] text-muted-foreground">{badge}</span>}
      {status && (
        <span className="rounded-full bg-allow/15 px-1.5 py-0.5 text-[10px] font-medium text-allow">
          {status}
        </span>
      )}
    </NavLink>
  )
}

export function AppShell() {
  return (
    <div className="flex h-screen bg-background text-foreground">
      <aside className="flex w-[220px] flex-col border-r border-border bg-card">
        <div className="flex h-14 items-center gap-2 px-4 border-b border-border">
          <div className="flex size-7 items-center justify-center rounded-md bg-primary text-primary-foreground">
            <Shield className="size-4" />
          </div>
          <span className="text-sm font-semibold">Fortify</span>
        </div>

        <nav className="flex-1 overflow-y-auto p-3">
          <div className="mb-4">
            <div className="px-3 pb-2 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
              Workspace
            </div>
            <div className="flex flex-col gap-0.5">
              {workspaceLinks.map((l) => (
                <NavItem key={l.to} {...l} />
              ))}
            </div>
          </div>

          <div>
            <div className="px-3 pb-2 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
              Environment
            </div>
            <div className="flex flex-col gap-0.5">
              {environmentLinks.map((l) => (
                <NavItem key={l.to} {...l} />
              ))}
            </div>
          </div>
        </nav>

        <div className="border-t border-border p-3 text-[11px] text-muted-foreground space-y-1">
          <div className="flex items-center gap-1.5">
            <span className="relative flex size-1.5">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary opacity-50" />
              <span className="relative inline-flex size-1.5 rounded-full bg-primary" />
            </span>
            Serving bundle <span className="font-mono text-foreground">v1</span>
          </div>
          <div>Pushed just now</div>
          <div>local · 1 region</div>
        </div>
      </aside>

      <div className="flex flex-1 flex-col">
        <header className="flex h-14 items-center justify-between border-b border-border px-6">
          <div className="flex items-center gap-3">
            <span className="inline-flex items-center gap-2 rounded-md border border-border bg-card px-2.5 py-1 text-xs">
              <span className="size-1.5 rounded-full bg-allow" />
              Project <span className="font-mono text-foreground">support-bot</span>
              <span className="text-muted-foreground">·</span>
              <span className="text-muted-foreground">production</span>
            </span>
          </div>
          <div className="flex items-center gap-3 text-muted-foreground">
            <span className="size-8 rounded-full bg-primary/20 text-primary grid place-items-center text-xs font-medium">
              MG
            </span>
          </div>
        </header>

        <main className="flex-1 overflow-y-auto px-8 py-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
