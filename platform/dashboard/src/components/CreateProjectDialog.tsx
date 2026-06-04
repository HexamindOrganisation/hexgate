import { useEffect } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'

import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useActive } from '@/lib/active'
import { useCreateProject } from '@/lib/projects'

const CreateProjectSchema = z.object({
  name: z.string().min(1, 'Required').max(64, 'Max 64 characters'),
})

type CreateProjectValues = z.infer<typeof CreateProjectSchema>

interface CreateProjectDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Override the org to create in. Defaults to the currently-active
   * org from the store. Useful for "Create project in Acme" from the
   * org list page where the active org might be elsewhere. */
  orgId?: string
  onCreated?: (projectId: string) => void
}

export function CreateProjectDialog({
  open,
  onOpenChange,
  orgId,
  onCreated,
}: CreateProjectDialogProps) {
  const activeOrgId = useActive((s) => s.activeOrgId)
  const setActiveProject = useActive((s) => s.setActiveProject)
  const create = useCreateProject()
  const targetOrgId = orgId ?? activeOrgId

  const form = useForm<CreateProjectValues>({
    resolver: zodResolver(CreateProjectSchema),
    defaultValues: { name: '' },
  })

  useEffect(() => {
    if (open) {
      form.reset({ name: '' })
      create.reset()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  async function onSubmit(values: CreateProjectValues) {
    if (!targetOrgId) return  // shouldn't happen: button is hidden when no org
    try {
      const project = await create.mutateAsync({
        orgId: targetOrgId,
        name: values.name,
      })
      // New project becomes active in the current org. If the user is
      // creating cross-org from the orgs list, this implicitly moves
      // them into that org's project list — usually what they want.
      setActiveProject(project.id)
      onCreated?.(project.id)
      onOpenChange(false)
    } catch {
      // surfaces via create.error
    }
  }

  const error = create.error
  const detailText = extractDetail(error)
  const nameTaken = detailText.toLowerCase().includes('already exists')

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create project</DialogTitle>
          <DialogDescription>
            Projects hold agents and the tokens that authenticate them.
            Names are unique within an organization.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={form.handleSubmit(onSubmit)}
          className="space-y-4"
          id="create-project-form"
        >
          {create.isError && (
            <Alert variant="destructive">
              <AlertDescription>
                {nameTaken
                  ? 'A project with that name already exists in this organization.'
                  : detailText || 'Could not create the project.'}
              </AlertDescription>
            </Alert>
          )}

          <div className="space-y-2">
            <Label htmlFor="project-name">Name</Label>
            <Input
              id="project-name"
              placeholder="customer-bot"
              autoComplete="off"
              {...form.register('name')}
            />
            {form.formState.errors.name && (
              <p className="text-xs text-destructive">
                {form.formState.errors.name.message}
              </p>
            )}
          </div>
        </form>

        <DialogFooter>
          <Button
            type="button"
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={create.isPending}
          >
            Cancel
          </Button>
          <Button
            type="submit"
            form="create-project-form"
            disabled={create.isPending || !targetOrgId}
          >
            {create.isPending ? 'Creating…' : 'Create project'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function extractDetail(err: unknown): string {
  if (err && typeof err === 'object' && 'detail' in err) {
    const d = (err as { detail: unknown }).detail
    if (typeof d === 'string') return d
    if (typeof d === 'object' && d !== null && 'detail' in d) {
      const inner = (d as { detail: unknown }).detail
      if (typeof inner === 'string') return inner
    }
  }
  return ''
}
