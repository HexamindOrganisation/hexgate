import { useState } from 'react'
import { FolderOpen, FolderPlus } from 'lucide-react'

import { CreateProjectDialog } from '@/components/CreateProjectDialog'
import { Button } from '@/components/ui/button'

/**
 * Rendered by project-scoped pages when ``useProjectScoped`` returns
 * ``no-project`` — either the user's active org has no projects yet,
 * or the org switcher is mid-transition and a default hasn't been
 * picked.
 *
 * Single CTA: open the create-project dialog (reused from the org
 * switcher footer). Keeps "you need a project" discoverable without
 * the user having to find the +New project button in the header
 * dropdown.
 */
interface NoProjectEmptyStateProps {
  /** Short noun naming the page's content — used in the help copy
   * ("…to view tokens", "…to view agents"). */
  resource: string
}

export function NoProjectEmptyState({ resource }: NoProjectEmptyStateProps) {
  const [createOpen, setCreateOpen] = useState(false)

  return (
    <div className="mx-auto flex max-w-md flex-col items-center gap-4 py-16 text-center">
      <div className="grid size-14 place-items-center rounded-full border border-border bg-card">
        <FolderOpen className="size-6 text-muted-foreground" />
      </div>
      <div className="space-y-1.5">
        <h2 className="text-base font-medium">No project selected</h2>
        <p className="text-sm text-muted-foreground">
          Pick a project from the switcher above, or create one to view
          this organization's {resource}.
        </p>
      </div>
      <Button onClick={() => setCreateOpen(true)} className="gap-2">
        <FolderPlus className="size-4" />
        Create project
      </Button>

      <CreateProjectDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
      />
    </div>
  )
}
