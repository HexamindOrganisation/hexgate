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
  MessageSquareCode,
  Network,
  PanelLeft,
  ScrollText,
  Settings2,
  Files,
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
import { useActive } from "@/lib/active";
import { useLogout, useUser } from "@/lib/auth";
import { useOrgs } from "@/lib/orgs";
import { useProjects } from "@/lib/projects";
import { useUi } from "@/lib/ui";
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

// The core product features. Org/account admin lives in the settings space
// (see `settingsLinks`), reached from the account menu — not this sidebar.
const workspaceLinks = [
  { to: "/agents", label: "Agents", icon: Bot },
  { to: "/policies", label: "Policies", icon: Files },
  { to: "/graph", label: "Graph", icon: Network },
  { to: "/playground", label: "Playground", icon: MessageSquareCode },
  { to: "/audit", label: "Audit", icon: ScrollText },
  { to: "/usage", label: "Usage", icon: BarChart3 },
  { to: "/bans", label: "Bans", icon: Ban },
  { to: "/tokens", label: "API keys", icon: KeyRound },
];

/** Whether a path belongs to the settings space (its own sidebar). */
function isSettingsPath(pathname: string): boolean {
  return pathname === "/settings" || pathname.startsWith("/orgs");
}

/** The settings-space sidebar. Member/settings links need the active org, so
 * they're built per-render; omitted when no org is selected. */
function settingsLinks(activeOrgId: string | null) {
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
  badge?: string;
  status?: string;
  collapsed?: boolean;
}) {
  return (
    <NavLink
      to={to}
      end={end}
      title={collapsed ? label : undefined}
      className={({ isActive }) =>
        cn(
          "flex h-9 items-center rounded-md text-sm transition-colors",
          collapsed ? "justify-center px-0" : "justify-between px-2",
          isActive
            ? "bg-primary/15 text-primary font-medium"
            : "text-muted-foreground hover:bg-accent hover:text-foreground",
        )
      }
    >
      <span className="flex items-center gap-2.5">
        <Icon className="size-4 shrink-0" />
        {!collapsed && label}
      </span>
      {!collapsed && badge && (
        <span className="text-[11px] text-muted-foreground">{badge}</span>
      )}
      {!collapsed && status && (
        <span className="rounded-full bg-allow/15 px-1.5 py-0.5 text-[10px] font-medium text-allow">
          {status}
        </span>
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
  const { pathname } = useLocation();
  const inSettings = isSettingsPath(pathname);
  const activeOrgId = useActive((s) => s.activeOrgId);
  const links = inSettings ? settingsLinks(activeOrgId) : workspaceLinks;

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

  return (
    <div className="flex h-screen bg-background text-foreground">
      <aside
        className={cn(
          // No hard right border — the card-vs-background shade separates the
          // sidebar from the content (VSCode "shades, not rules"). The whole
          // shell lives in the sidebar (switcher on top, account on the
          // bottom) so the content column reclaims the old header's height.
          "flex flex-col bg-card transition-[width] duration-200",
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
                  onClick={() => navigate("/agents")}
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
                to="/agents"
                label="Back to workspace"
                icon={ArrowLeft}
                collapsed
              />
            </div>
          )}
          <div className="flex flex-col gap-0.5">
            {links.map((l) => (
              <NavItem key={l.to} {...l} collapsed={sidebarCollapsed} />
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
