import { useState } from 'react'
import {
  Building2,
  Check,
  ChevronsUpDown,
  FolderPlus,
  Plus,
} from 'lucide-react'

import { CreateOrgDialog } from '@/components/CreateOrgDialog'
import { CreateProjectDialog } from '@/components/CreateProjectDialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useActive } from '@/lib/active'
import { useOrgs, type OrgWithRole } from '@/lib/orgs'
import { useProjects, type ProjectRead } from '@/lib/projects'
import { cn } from '@/lib/utils'

/**
 * The pill that lives in the AppShell header. Reads the active org +
 * project from the store, lists all the user's orgs (with their
 * projects nested) in a single dropdown. "+ New project" /
 * "+ New organization" footer actions open the corresponding dialogs.
 *
 * Two state pieces:
 *   - which orgs/projects exist (from React Query)
 *   - which is active (from the zustand store)
 *
 * Bootstrap (auto-pick first org + project when nothing's active) is
 * handled by the parent AppShell so this component stays pure.
 */
export function OrgProjectSwitcher() {
  const { activeOrgId, activeProjectId, setActiveOrg, setActiveProject } =
    useActive()
  const orgsQuery = useOrgs()
  const projectsQuery = useProjects(activeOrgId)

  const [createOrgOpen, setCreateOrgOpen] = useState(false)
  const [createProjectOpen, setCreateProjectOpen] = useState(false)

  const orgs: OrgWithRole[] = orgsQuery.data ?? []
  const projects: ProjectRead[] = projectsQuery.data ?? []
  const activeOrg = orgs.find((o) => o.id === activeOrgId) ?? null
  const activeProject = projects.find((p) => p.id === activeProjectId) ?? null

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className={cn(
              'inline-flex items-center gap-2 rounded-md border border-border bg-card px-2.5 py-1 text-xs',
              'transition-colors hover:border-primary hover:bg-primary/5',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
            )}
          >
            <Building2 className="h-3.5 w-3.5 text-primary" />
            <SwitcherLabel
              activeOrg={activeOrg}
              activeProject={activeProject}
              loading={orgsQuery.isLoading}
            />
            <ChevronsUpDown className="h-3 w-3 text-muted-foreground" />
          </button>
        </DropdownMenuTrigger>

        <DropdownMenuContent align="start" className="min-w-[260px]">
          <DropdownMenuLabel>Organizations</DropdownMenuLabel>
          {orgs.length === 0 ? (
            <div className="px-2 py-3 text-xs text-muted-foreground">
              Loading…
            </div>
          ) : (
            orgs.map((org) => (
              <DropdownMenuItem
                key={org.id}
                onSelect={() => setActiveOrg(org.id)}
                className="flex items-center gap-2"
              >
                <span className="flex-1 truncate font-medium">{org.name}</span>
                <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                  {org.role}
                </span>
                {org.id === activeOrgId && (
                  <Check className="h-3.5 w-3.5 text-primary" />
                )}
              </DropdownMenuItem>
            ))
          )}

          <DropdownMenuSeparator />

          <DropdownMenuLabel>
            Projects{activeOrg ? ` in ${activeOrg.name}` : ''}
          </DropdownMenuLabel>
          {activeOrgId === null ? (
            <div className="px-2 py-3 text-xs text-muted-foreground">
              Pick an organization first.
            </div>
          ) : projectsQuery.isLoading ? (
            <div className="px-2 py-3 text-xs text-muted-foreground">
              Loading…
            </div>
          ) : projects.length === 0 ? (
            <div className="px-2 py-3 text-xs text-muted-foreground">
              No projects yet.
            </div>
          ) : (
            projects.map((project) => (
              <DropdownMenuItem
                key={project.id}
                onSelect={() => setActiveProject(project.id)}
                className="flex items-center gap-2"
              >
                <span className="flex-1 truncate font-mono text-xs">
                  {project.name}
                </span>
                {project.id === activeProjectId && (
                  <Check className="h-3.5 w-3.5 text-primary" />
                )}
              </DropdownMenuItem>
            ))
          )}

          <DropdownMenuSeparator />

          <DropdownMenuItem
            onSelect={() => setCreateProjectOpen(true)}
            disabled={!activeOrgId}
          >
            <FolderPlus className="h-3.5 w-3.5" />
            <span>New project</span>
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => setCreateOrgOpen(true)}>
            <Plus className="h-3.5 w-3.5" />
            <span>New organization</span>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <CreateOrgDialog
        open={createOrgOpen}
        onOpenChange={setCreateOrgOpen}
      />
      <CreateProjectDialog
        open={createProjectOpen}
        onOpenChange={setCreateProjectOpen}
      />
    </>
  )
}

interface SwitcherLabelProps {
  activeOrg: OrgWithRole | null
  activeProject: ProjectRead | null
  loading: boolean
}

function SwitcherLabel({
  activeOrg,
  activeProject,
  loading,
}: SwitcherLabelProps) {
  if (loading) {
    return <span className="text-muted-foreground">Loading…</span>
  }
  if (!activeOrg) {
    return <span className="text-muted-foreground">Pick an organization</span>
  }
  return (
    <span className="flex items-center gap-1.5">
      <span className="text-foreground">{activeOrg.name}</span>
      {activeProject && (
        <>
          <span className="text-muted-foreground">/</span>
          <span className="font-mono text-foreground">{activeProject.name}</span>
        </>
      )}
      {!activeProject && (
        <span className="text-muted-foreground">· no project</span>
      )}
    </span>
  )
}
