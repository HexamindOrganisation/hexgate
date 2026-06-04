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
import { useCreateOrg } from '@/lib/orgs'

/** Mirror of the backend slug regex (schemas.OrgCreate). DNS-label
 * shape — lowercase letters/digits/hyphens, must start with a letter,
 * can't end with hyphen, 1-32 chars. Single letter accepted. */
const SLUG_REGEX = /^([a-z]|[a-z][a-z0-9-]*[a-z0-9])$/

const CreateOrgSchema = z.object({
  name: z.string().min(1, 'Required').max(64, 'Max 64 characters'),
  // Empty string is allowed at the form layer (means "let the server
  // derive it from the name"); we strip it before submitting.
  slug: z
    .string()
    .max(32, 'Max 32 characters')
    .refine((v) => v === '' || SLUG_REGEX.test(v), {
      message: 'Lowercase letters, digits, hyphens. Must start with a letter.',
    })
    .optional(),
})

type CreateOrgValues = z.infer<typeof CreateOrgSchema>

interface CreateOrgDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Optional callback after a successful create — receives the new
   * org id so the caller can navigate or set it active. The dialog
   * itself sets it active automatically (the most common UX). */
  onCreated?: (orgId: string) => void
}

export function CreateOrgDialog({
  open,
  onOpenChange,
  onCreated,
}: CreateOrgDialogProps) {
  const create = useCreateOrg()
  const setActiveOrg = useActive((s) => s.setActiveOrg)

  const form = useForm<CreateOrgValues>({
    resolver: zodResolver(CreateOrgSchema),
    defaultValues: { name: '', slug: '' },
  })

  // Reset the form + mutation error each time the dialog opens. Without
  // this, closing the dialog mid-error and reopening shows stale state.
  useEffect(() => {
    if (open) {
      form.reset({ name: '', slug: '' })
      create.reset()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  async function onSubmit(values: CreateOrgValues) {
    try {
      const org = await create.mutateAsync({
        name: values.name,
        slug: values.slug || undefined,
      })
      // The brand-new org becomes the active one — the user's intent
      // when creating is to use it next.
      setActiveOrg(org.id)
      onCreated?.(org.id)
      onOpenChange(false)
    } catch {
      // surfaces via create.error
    }
  }

  const error = create.error
  const detailText = extractDetail(error)
  const slugTaken = detailText.toLowerCase().includes('taken')

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create organization</DialogTitle>
          <DialogDescription>
            Teams in HexaGate live inside an organization. You'll be its
            first owner.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={form.handleSubmit(onSubmit)}
          className="space-y-4"
          id="create-org-form"
        >
          {create.isError && (
            <Alert variant="destructive">
              <AlertDescription>
                {slugTaken
                  ? 'That slug is already taken. Pick a different one.'
                  : detailText || 'Could not create the organization.'}
              </AlertDescription>
            </Alert>
          )}

          <div className="space-y-2">
            <Label htmlFor="org-name">Name</Label>
            <Input
              id="org-name"
              placeholder="Acme Inc"
              autoComplete="off"
              {...form.register('name')}
            />
            {form.formState.errors.name && (
              <p className="text-xs text-destructive">
                {form.formState.errors.name.message}
              </p>
            )}
          </div>

          <div className="space-y-2">
            <Label htmlFor="org-slug">
              Slug{' '}
              <span className="text-xs font-normal text-muted-foreground">
                (optional — we'll generate one if blank)
              </span>
            </Label>
            <Input
              id="org-slug"
              placeholder="acme"
              autoComplete="off"
              {...form.register('slug')}
            />
            {form.formState.errors.slug && (
              <p className="text-xs text-destructive">
                {form.formState.errors.slug.message}
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
            form="create-org-form"
            disabled={create.isPending}
          >
            {create.isPending ? 'Creating…' : 'Create organization'}
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
