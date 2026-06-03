import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { toast } from 'sonner'
import { AlertTriangle, ArrowLeft } from 'lucide-react'

import { ConfirmDialog } from '@/components/ConfirmDialog'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ApiError } from '@/lib/api'
import { useActive } from '@/lib/active'
import { useUser } from '@/lib/auth'
import { useLeaveOrg } from '@/lib/members'
import { useOrgs, useUpdateOrg } from '@/lib/orgs'

/** Mirror of CreateOrgDialog's slug regex (DNS-label shape). */
const SLUG_REGEX = /^([a-z]|[a-z][a-z0-9-]*[a-z0-9])$/

const SettingsSchema = z.object({
  name: z.string().min(1, 'Required').max(64, 'Max 64 characters'),
  slug: z
    .string()
    .min(1, 'Required')
    .max(32, 'Max 32 characters')
    .regex(
      SLUG_REGEX,
      'Lowercase letters, digits, hyphens. Must start with a letter.',
    ),
})

type SettingsValues = z.infer<typeof SettingsSchema>

/**
 * `/orgs/:orgId/settings` — name + slug editing + the "Leave organization"
 * danger zone.
 *
 * Role-aware:
 *   - Plain members see read-only inputs + an explanatory line
 *   - Admin/owner can edit + save
 *   - "Leave" is enabled regardless of role; the backend's
 *     last-owner guard returns 409 which we surface as a friendly
 *     toast ("you're the only owner; promote someone first")
 */
export function OrgSettingsPage() {
  const { orgId } = useParams<{ orgId: string }>()
  const navigate = useNavigate()
  const { user } = useUser()
  const orgsQuery = useOrgs()
  const updateOrg = useUpdateOrg()
  const leaveOrg = useLeaveOrg()
  const setActiveOrg = useActive((s) => s.setActiveOrg)
  const [confirmLeaveOpen, setConfirmLeaveOpen] = useState(false)

  const org = orgsQuery.data?.find((o) => o.id === orgId) ?? null
  const canEdit = org?.role === 'owner' || org?.role === 'admin'

  const form = useForm<SettingsValues>({
    resolver: zodResolver(SettingsSchema),
    defaultValues: { name: '', slug: '' },
  })

  // Reset the form whenever the active org row changes — switching orgs
  // via the switcher while sitting on this page should re-populate the
  // fields with the new org's values.
  useEffect(() => {
    if (org) {
      form.reset({ name: org.name, slug: org.slug })
      updateOrg.reset()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [org?.id, org?.name, org?.slug])

  if (orgsQuery.isLoading) {
    return (
      <div className="mx-auto max-w-3xl p-6 text-sm text-muted-foreground">
        Loading…
      </div>
    )
  }

  if (!org) {
    // Either the org doesn't exist or the user isn't a member.
    // Backend would 403/404 the underlying API; here we surface the
    // friendlier message without an API round-trip.
    return (
      <div className="mx-auto max-w-3xl space-y-4 p-6">
        <Button asChild variant="ghost" size="sm">
          <a href="/orgs" onClick={(e) => { e.preventDefault(); navigate('/orgs') }}>
            <ArrowLeft className="h-3.5 w-3.5" />
            Back to organizations
          </a>
        </Button>
        <Alert variant="destructive">
          <AlertDescription>
            Organization not found, or you no longer have access to it.
          </AlertDescription>
        </Alert>
      </div>
    )
  }

  async function onSubmit(values: SettingsValues): Promise<void> {
    if (!org) return  // narrows the type below
    try {
      await updateOrg.mutateAsync({
        orgId: org.id,
        name: values.name !== org.name ? values.name : undefined,
        slug: values.slug !== org.slug ? values.slug : undefined,
      })
      toast.success('Organization saved')
    } catch (err) {
      const detail = err instanceof ApiError ? String(err.message) : ''
      const slugTaken = detail.toLowerCase().includes('taken')
      toast.error(
        slugTaken
          ? 'That slug is already in use. Try a different one.'
          : 'Could not save changes.',
      )
    }
  }

  async function onLeave(): Promise<void> {
    if (!user || !org) return
    try {
      await leaveOrg.mutateAsync({ orgId: org.id, userId: user.id })
      toast.success(`Left "${org.name}"`)
      // Drop active-org so the bootstrap in AppShell picks a fresh one.
      setActiveOrg(null)
      navigate('/orgs', { replace: true })
    } catch (err) {
      const detail = err instanceof ApiError ? String(err.message) : ''
      const lastOwner = detail.toLowerCase().includes('last owner')
      toast.error(
        lastOwner
          ? "You're the only owner — promote someone to owner before leaving."
          : 'Could not leave the organization.',
      )
    } finally {
      setConfirmLeaveOpen(false)
    }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <Button
        variant="ghost"
        size="sm"
        onClick={() => navigate('/orgs')}
        className="-ml-2"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        Back to organizations
      </Button>

      <Card>
        <CardHeader>
          <CardTitle>Organization settings</CardTitle>
          <CardDescription>
            <span className="font-medium text-foreground">{org.name}</span>
            <span className="text-muted-foreground"> · </span>
            <span className="font-mono">{org.slug}</span>
            <span className="text-muted-foreground"> · your role: </span>
            <span className="capitalize text-foreground">{org.role}</span>
          </CardDescription>
        </CardHeader>

        <form onSubmit={form.handleSubmit(onSubmit)} id="org-settings-form">
          <CardContent className="space-y-4">
            {!canEdit && (
              <Alert>
                <AlertDescription>
                  View-only — only admins and owners can edit this
                  organization's name or slug.
                </AlertDescription>
              </Alert>
            )}

            <div className="space-y-2">
              <Label htmlFor="org-name">Name</Label>
              <Input
                id="org-name"
                disabled={!canEdit}
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
              <Label htmlFor="org-slug">Slug</Label>
              <Input
                id="org-slug"
                disabled={!canEdit}
                autoComplete="off"
                {...form.register('slug')}
              />
              <p className="text-xs text-muted-foreground">
                Used in URLs. Changing this breaks existing bookmarks.
              </p>
              {form.formState.errors.slug && (
                <p className="text-xs text-destructive">
                  {form.formState.errors.slug.message}
                </p>
              )}
            </div>
          </CardContent>

          {canEdit && (
            <CardFooter className="justify-end">
              <Button
                type="submit"
                disabled={updateOrg.isPending || !form.formState.isDirty}
              >
                {updateOrg.isPending ? 'Saving…' : 'Save changes'}
              </Button>
            </CardFooter>
          )}
        </form>
      </Card>

      <Card className="border-destructive/40">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <AlertTriangle className="h-4 w-4 text-destructive" />
            Danger zone
          </CardTitle>
          <CardDescription>
            Leaving removes your access to this organization's projects.
            You can't undo this — re-joining requires an invite.
          </CardDescription>
        </CardHeader>
        <CardFooter className="justify-end">
          <Button
            variant="destructive"
            onClick={() => setConfirmLeaveOpen(true)}
            disabled={leaveOrg.isPending}
          >
            Leave organization
          </Button>
        </CardFooter>
      </Card>

      <ConfirmDialog
        open={confirmLeaveOpen}
        onOpenChange={setConfirmLeaveOpen}
        title={`Leave ${org.name}?`}
        description={`You'll lose access to ${org.name}'s projects, tokens, and audit logs. An admin can re-invite you later.`}
        confirmLabel="Leave organization"
        confirmVariant="destructive"
        pending={leaveOrg.isPending}
        onConfirm={onLeave}
      />
    </div>
  )
}
