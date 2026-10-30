import { useEffect, useState } from "react";
import { useLocation, useNavigate, NavLink, Outlet } from "react-router-dom";
import {
  ArrowLeft,
  Ban,
  BarChart3,
  Building2,
  Bot,
  CircleUser,
  KeyRound,
  LogOut,
  Monitor,
  Moon,
  PanelLeft,
  Scale,
  ScrollText,
  Settings2,
  Files,
  House,
  Sun,
  Users,
  type LucideIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { OrgProjectSwitcher } from "@/components/OrgProjectSwitcher";
import { CreateOrgDialog } from "@/components/CreateOrgDialog";
import { CreateProjectDialog } from "@/components/CreateProjectDialog";
import { PreviewBanner } from "@/components/PreviewBanner";
import { VerifyEmailBanner } from "@/components/VerifyEmailBanner";
import { useActive, useProjectScoped } from "@/lib/active";
import { useBans, useCanManageBans } from "@/lib/bans";
import { useLogout, useUser } from "@/lib/auth";
import { useOrgs } from "@/lib/orgs";
import { useProjects } from "@/lib/projects";
import { useUi } from "@/lib/ui";
import { SCHEMES, schemeAccent, useIsDark, useTheme } from "@/lib/theme";
import { usePresentMode } from "@/lib/present";
import { cn } from "@/lib/utils";

/**
 * Bootstrap effect — runs on every AppShell mount.
 *
 * If no active org is set (first-ever visit or post-logout sign-in),
 * pick the user's first org. Once an org is active, if no project is
 * set, pick its first project (or null if the org is empty). Keeps
 * the switcher in a usable default state so a freshly-signed-up user
 * immediately sees their own data, not an empty/error state.
 *
 * Idempotent — won't overwrite a valid existing selection.
 */
function useActiveBootstrap(): void {
  const { activeOrgId, activeProjectId, setActiveOrg, setActiveProject } =
    useActive();
  const orgsQuery = useOrgs();
  const projectsQuery = useProjects(activeOrgId);

  // First-org bootstrap. Don't run while orgs are loading — we'd
  // briefly set null and flicker the switcher label.
  useEffect(() => {
    if (orgsQuery.isLoading || !orgsQuery.data) return;
    if (activeOrgId === null) {
      const first = orgsQuery.data[0];
      if (first) setActiveOrg(first.id);
      return;
    }
    // Stale-org cleanup: the persisted activeOrgId refers to an org
    // the user no longer belongs to (e.g., they got removed). Reset
    // to the first remaining one.
    if (!orgsQuery.data.some((o) => o.id === activeOrgId)) {
      const fallback = orgsQuery.data[0] ?? null;
      setActiveOrg(fallback?.id ?? null);
    }
  }, [orgsQuery.isLoading, orgsQuery.data, activeOrgId, setActiveOrg]);

  // First-project bootstrap, scoped to the active org. setActiveOrg
  // clears activeProjectId in the store so we'll always come through
  // here after an org change.
  useEffect(() => {
    if (!activeOrgId || projectsQuery.isLoading || !projectsQuery.data) return;
    if (activeProjectId === null) {
      const first = projectsQuery.data[0];
      if (first) setActiveProject(first.id);
      return;
    }
    // Stale-project cleanup (e.g., project deleted in another tab).
    if (!projectsQuery.data.some((p) => p.id === activeProjectId)) {
      const fallback = projectsQuery.data[0] ?? null;
      setActiveProject(fallback?.id ?? null);
    }
  }, [
    activeOrgId,
    activeProjectId,
    projectsQuery.isLoading,
    projectsQuery.data,
    setActiveProject,
  ]);
}

interface NavLinkDef {
  to: string;
  label: string;
  icon: LucideIcon;
  end?: boolean;
  /** Which live count, if any, this link shows. */
  count?: "activeBans";
}

interface NavGroup {
  /** Section heading; omitted for the top group (Home). */
  label?: string;
  links: NavLinkDef[];
}

// The core product features, grouped by job. Org/account admin lives in the
// settings space (see `settingsLinks`), reached from the account menu.
const workspaceGroups: NavGroup[] = [
  { links: [{ to: "/", label: "Home", icon: House, end: true }] },
  {
    label: "Build",
    links: [
      { to: "/agents", label: "Agents", icon: Bot },
      { to: "/policies", label: "Policies", icon: Files },
    ],
  },
  {
    label: "Monitor",
    links: [
      { to: "/audit", label: "Audit", icon: ScrollText },
      { to: "/usage", label: "Usage", icon: BarChart3 },
    ],
  },
  {
    label: "Control",
    links: [
      { to: "/bans", label: "Bans", icon: Ban, count: "activeBans" },
      // One word, like every other entry here.
      { to: "/ai-act", label: "Governance", icon: Scale },
      { to: "/tokens", label: "API keys", icon: KeyRound },
    ],
  },
];

/** Whether a path belongs to the settings space (its own sidebar). Uses a
 * segment boundary so a future sibling like `/orgstats` isn't misread. */
function isSettingsPath(pathname: string): boolean {
  return (
    pathname === "/settings" ||
    pathname === "/orgs" ||
    pathname.startsWith("/orgs/")
  );
}

/** The org the settings sidebar should scope its org links to: the one in the
 * URL (`/orgs/:id/...`) when present, else the active org — so a deep-link or
 * refresh keeps the sidebar pointed at the org being viewed. On the pure
 * account page (`/settings`) there's no org in play, so we return null and the
 * org-scoped links drop out. */
function settingsOrgId(
  pathname: string,
  activeOrgId: string | null,
): string | null {
  if (pathname === "/settings") return null;
  return pathname.match(/^\/orgs\/([^/]+)(?:\/|$)/)?.[1] ?? activeOrgId;
}

/** The settings-space sidebar. Member/settings links need the active org, so
 * they're built per-render; omitted when no org is selected. */
function settingsLinks(activeOrgId: string | null): NavLinkDef[] {
  return [
    { to: "/orgs", label: "Organizations", icon: Building2, end: true },
    ...(activeOrgId
      ? [
          {
            to: `/orgs/${activeOrgId}/members`,
            label: "Members",
            icon: Users,
          },
          {
            to: `/orgs/${activeOrgId}/settings`,
            label: "Organization settings",
            icon: Settings2,
          },
        ]
      : []),
    { to: "/settings", label: "Account", icon: CircleUser },
  ];
}

function NavItem({
  to,
  label,
  icon: Icon,
  end,
  badge,
  status,
  collapsed,
}: {
  to: string;
  label: string;
  icon: LucideIcon;
  end?: boolean;
  /** A count that needs attention; `noun` names it for the accessible label. */
  badge?: { value: number; noun: string };
  status?: string;
  collapsed?: boolean;
}) {
  return (
    <NavLink
      to={to}
      end={end}
      title={collapsed ? label : undefined}
      aria-label={badge ? `${label}, ${badge.value} ${badge.noun}` : undefined}
      className={({ isActive }) =>
        cn(
          "flex h-9 items-center rounded-lg text-sm transition-colors",
          collapsed ? "justify-center px-0" : "justify-between px-2.5",
          isActive
            ? "bg-card font-semibold text-foreground shadow-xs ring-1 ring-border"
            : "font-medium text-muted-foreground hover:bg-accent hover:text-foreground",
        )
      }
    >
      {({ isActive }) => (
        <>
          <span className="relative flex items-center gap-2.5">
            <Icon
              className={cn("size-4 shrink-0", isActive && "text-primary")}
            />
            {!collapsed && label}
            {/* Collapsed: a dot on the icon keeps the signal visible. */}
            {collapsed && badge && (
              <span className="absolute -top-0.5 left-3 size-2 rounded-full bg-deny ring-2 ring-background" />
            )}
          </span>
          {badge && (
            <span
              className={cn(
                "grid h-[18px] min-w-[18px] place-items-center rounded-full bg-deny/15 px-1.5 text-[11px] font-semibold text-deny",
                collapsed && "sr-only",
              )}
            >
              {badge.value}
            </span>
          )}
          {!collapsed && status && (
            <span className="rounded-full bg-success/15 px-1.5 py-0.5 text-[10px] font-medium text-success">
              {status}
            </span>
          )}
        </>
      )}
    </NavLink>
  );
}

export function AppShell() {
  // Pick a default active org + project on first load so the switcher
  // shows something usable instead of "Pick an organization" empty
  // state. Idempotent — won't overwrite an existing valid selection.
  useActiveBootstrap();
  const { sidebarCollapsed, toggleSidebar } = useUi();
  const navigate = useNavigate();

  // Two sidebar "spaces" share this one shell frame: the product features, and
  // an org/account settings space (its own nav), entered from the account menu.
  const location = useLocation();
  const { pathname } = location;
  // Full-screen "presentation" mode (opened in its own tab from the Playground):
  // render just the route, no sidebar/nav chrome. Detected from the URL so it
  // survives a reload of the presentation tab.
  const present = usePresentMode();
  const inSettings = isSettingsPath(pathname);
  const activeOrgId = useActive((s) => s.activeOrgId);
  // Scope the settings links to the URL's org (falling back to the active one)
  // so a refresh/deep-link to /orgs/:id/... keeps the sidebar on that org.
  const groups: NavGroup[] = inSettings
    ? [{ links: settingsLinks(settingsOrgId(pathname, activeOrgId)) }]
    : workspaceGroups;
  // Active bans need attention, so the nav shows their count. Admin-only
  // endpoint, and only once the project scope has resolved.
  const canManageBans = useCanManageBans();
  const scope = useProjectScoped();
  const bans = useBans(
    // projectName is only set once the id is found in the projects list, so
    // a stale (deleted) persisted id never gets polled.
    !inSettings && canManageBans && scope.projectName !== null
      ? scope.projectId
      : null,
    { live: true },
  );
  const counts: Record<
    NonNullable<NavLinkDef["count"]>,
    { value: number; noun: string } | undefined
  > = {
    activeBans: bans.data?.length
      ? { value: bans.data.length, noun: "active bans" }
      : undefined,
  };

  // The workspace create-dialogs live here, not in OrgProjectSwitcher, so
  // collapsing the sidebar (which unmounts the switcher trigger) can't unmount
  // an open dialog and drop the user's half-typed form.
  const [createOrgOpen, setCreateOrgOpen] = useState(false);
  const [createProjectOpen, setCreateProjectOpen] = useState(false);

  // Cmd/Ctrl-B toggles the sidebar (standard editor shortcut). toggleSidebar is
  // a stable zustand action, so this binds once. Guard the match: ignore key
  // repeat and extra modifiers, and — crucially — presses inside an editable
  // target (an input, or the CodeMirror policy editor, which binds Ctrl-B to
  // move-cursor-left) so the shortcut never hijacks typing.
  useEffect(() => {
    const isEditable = (t: EventTarget | null): boolean =>
      t instanceof HTMLElement &&
      (t.isContentEditable ||
        ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName) ||
        t.closest(".cm-editor") !== null);
    const onKey = (e: KeyboardEvent) => {
      if (e.repeat || e.altKey || e.shiftKey) return;
      if (!(e.metaKey || e.ctrlKey) || e.key.toLowerCase() !== "b") return;
      if (isEditable(e.target)) return;
      e.preventDefault();
      toggleSidebar();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggleSidebar]);

  if (present) {
    // No sidebar, no nav — just the route, full-bleed. The playground drops its
    // negative margins in this mode and renders its own close affordance.
    return (
      <div className="h-screen bg-background text-foreground">
        <Outlet />
      </div>
    );
  }

  return (
    <div className="flex h-screen bg-background text-foreground">
      <aside
        className={cn(
          // Same ground as the page plus a hairline, so the active item can
          // sit on it as a raised card. The whole shell lives in the sidebar
          // (switcher on top, account on the bottom), so the content column
          // reclaims the old header's height.
          "flex flex-col border-r border-border bg-background transition-[width] duration-200",
          sidebarCollapsed ? "w-14" : "w-[240px]",
        )}
      >
        {/* Top: project/org switcher (two lines) + the collapse toggle,
            top-aligned so it sits level with the project line. */}
        <div
          className={cn(
            "flex items-start gap-1 overflow-hidden py-2",
            sidebarCollapsed ? "justify-center px-0" : "px-2",
          )}
        >
          {!sidebarCollapsed && (
            <div className="min-w-0 flex-1">
              {inSettings ? (
                <button
                  type="button"
                  onClick={() => navigate("/")}
                  className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <ArrowLeft className="size-4" />
                  Back to workspace
                </button>
              ) : (
                <OrgProjectSwitcher
                  onNewOrg={() => setCreateOrgOpen(true)}
                  onNewProject={() => setCreateProjectOpen(true)}
                />
              )}
            </div>
          )}
          <Button
            variant="ghost"
            size="icon"
            className="size-8 shrink-0 text-muted-foreground"
            title={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-label={
              sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"
            }
            onClick={toggleSidebar}
          >
            <PanelLeft className="size-4" />
          </Button>
        </div>

        <nav className="flex-1 overflow-y-auto px-2 py-2 scrollbar-thin">
          {/* Collapsed settings space has no switcher line, so surface the exit
              as a nav row too. */}
          {inSettings && sidebarCollapsed && (
            <div className="mb-0.5 flex flex-col gap-0.5">
              <NavItem
                to="/"
                end
                label="Back to workspace"
                icon={ArrowLeft}
                collapsed
              />
            </div>
          )}
          <div className="flex flex-col gap-4">
            {groups.map((g, i) => (
              <div key={g.label ?? i} className="flex flex-col gap-0.5">
                {g.label && !sidebarCollapsed && (
                  <div className="px-2.5 pb-1 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                    {g.label}
                  </div>
                )}
                {g.links.map(({ count, ...l }) => (
                  <NavItem
                    key={l.to}
                    {...l}
                    badge={count && counts[count]}
                    collapsed={sidebarCollapsed}
                  />
                ))}
              </div>
            ))}
          </div>
        </nav>

        <AccountChip collapsed={sidebarCollapsed} />
      </aside>

      <div className="flex flex-1 flex-col">
        <PreviewBanner />
        <VerifyEmailBanner />

        <main className="flex-1 overflow-y-auto px-8 py-6 scrollbar-thin">
          <Outlet />
        </main>
      </div>

      {/* Workspace dialogs live at the shell level so their lifecycle is
          independent of the sidebar's collapse state. */}
      <CreateOrgDialog open={createOrgOpen} onOpenChange={setCreateOrgOpen} />
      <CreateProjectDialog
        open={createProjectOpen}
        onOpenChange={setCreateProjectOpen}
      />
    </div>
  );
}

/**
 * Bottom-of-sidebar account chip — the signed-in user, opening a menu
 * upward with settings shortcuts and sign-out (OpenAI-style). The project
 * and org now live in the top switcher, so this is purely the user's
 * identity. When the sidebar is collapsed it shrinks to just the avatar.
 */
function AccountChip({ collapsed }: { collapsed: boolean }) {
  const { user } = useUser();
  const logout = useLogout();
  const navigate = useNavigate();

  if (!user) return null;

  const username = user.email.split("@")[0] || user.email;
  const initial = (user.email.slice(0, 1) || "?").toUpperCase();

  return (
    <div className="p-2">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            title={collapsed ? user.email : undefined}
            className={cn(
              "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-accent",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              collapsed && "justify-center",
            )}
          >
            <span className="grid size-7 shrink-0 place-items-center rounded-full bg-primary/20 text-xs font-medium text-primary">
              {initial}
            </span>
            {!collapsed && (
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium">
                  {username}
                </span>
                <span className="block truncate text-xs text-muted-foreground">
                  {user.email}
                </span>
              </span>
            )}
          </button>
        </DropdownMenuTrigger>

        <DropdownMenuContent align="start" side="top" className="min-w-[240px]">
          <DropdownMenuLabel className="truncate text-xs font-normal text-muted-foreground">
            {user.email}
          </DropdownMenuLabel>
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => navigate("/settings")}>
            <Settings2 className="size-4" />
            <span>Settings</span>
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => navigate("/orgs")}>
            <Building2 className="size-4" />
            <span>Organizations</span>
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <AppearanceControl />
          <DropdownMenuSeparator />
          <DropdownMenuItem
            disabled={logout.isPending}
            onSelect={async () => {
              await logout.mutateAsync().catch(() => undefined);
              navigate("/sign-in", { replace: true });
            }}
          >
            <LogOut className="size-4" />
            <span>Log out</span>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

const MODES = [
  ["light", Sun, "Light"],
  ["dark", Moon, "Dark"],
  ["system", Monitor, "System"],
] as const;

/** Appearance picker inside the account menu: mode (light/dark/system) + accent
 * scheme. Plain buttons so a pick doesn't close the menu. */
function AppearanceControl() {
  const mode = useTheme((s) => s.mode);
  const scheme = useTheme((s) => s.scheme);
  const setMode = useTheme((s) => s.setMode);
  const setScheme = useTheme((s) => s.setScheme);
  // Reactive, so the dots follow an OS light/dark flip while mode is "system".
  const dark = useIsDark();

  return (
    <div className="space-y-3 px-2 py-2">
      <div className="text-caption font-semibold uppercase tracking-wider text-muted-foreground">
        Appearance
      </div>
      <div className="grid grid-cols-3 gap-0.5 rounded-lg bg-muted p-0.5">
        {MODES.map(([m, Icon, label]) => (
          <button
            key={m}
            type="button"
            aria-pressed={mode === m}
            onClick={() => setMode(m)}
            className={cn(
              "flex h-7 items-center justify-center gap-1.5 rounded-md text-xs font-medium text-muted-foreground transition-colors hover:text-foreground",
              mode === m && "bg-raised text-foreground shadow-xs",
            )}
          >
            <Icon className="size-3.5" />
            {label}
          </button>
        ))}
      </div>
      <div>
        <div className="mb-1.5 text-xs font-medium text-muted-foreground">
          Accent
        </div>
        <div className="flex gap-2">
          {SCHEMES.map(({ id, label }) => (
            <button
              key={id}
              type="button"
              title={label}
              aria-label={`${label} accent`}
              aria-pressed={scheme === id}
              onClick={() => setScheme(id)}
              className={cn(
                "size-6 rounded-full transition-shadow",
                scheme === id &&
                  "ring-2 ring-foreground ring-offset-2 ring-offset-popover",
              )}
              style={{ backgroundColor: `hsl(${schemeAccent(id, dark)})` }}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
