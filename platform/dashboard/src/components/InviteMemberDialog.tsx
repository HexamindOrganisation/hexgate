import { useEffect } from 'react'
import { useForm, Controller } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { toast } from 'sonner'

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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { ApiError } from '@/lib/api'
import { useCreateInvitation } from '@/lib/invites'
import { ROLES } from '@/lib/orgs'

const InviteSchema = z.object({
  email: z.string().email('Enter a valid email'),
  role: z.enum(ROLES),
})

type InviteValues = z.infer<typeof InviteSchema>

interface InviteMemberDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Org the invite is for. The dialog ALWAYS targets the explicitly-
   * passed org — never reads from the active store. Lets the caller
   * surface "Invite to Acme Inc" copy even when the user is sitting
   * on a different org's page. */
  orgId: string
}

/**
 * "Invite member" form. Email + role-select. On success, sonner
 * toast confirms; on failure, parses the backend ApiError detail
 * to surface specific messages for role-escalation rejection and
 * generic 400s.
 */
export function InviteMemberDialog({
  open,
  onOpenChange,
  orgId,
}: InviteMemberDialogProps) {
  const invite = useCreateInvitation()

  const form = useForm<InviteValues>({
    resolver: zodResolver(InviteSchema),
    defaultValues: { email: '', role: 'member' },
  })

  useEffect(() => {
    if (open) {
      form.reset({ email: '', role: 'member' })
      invite.reset()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  async function onSubmit(values: InviteValues): Promise<void> {
    try {
      await invite.mutateAsync({
        orgId,
        email: values.email,
        role: values.role,
      })
      toast.success(`Invitation sent to ${values.email}`)
      onOpenChange(false)
    } catch (err) {
      const detail = err instanceof ApiError ? String(err.message) : ''
      const roleEscalation = detail.toLowerCase().includes('cannot invite')
      toast.error(
        roleEscalation
          ? `You can't invite at the ${values.role} level (your role is below that).`
          : 'Could not send the invitation. Check the email and try again.',
      )
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Invite a teammate</DialogTitle>
          <DialogDescription>
            We'll email them a magic link to join this organization.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={form.handleSubmit(onSubmit)}
          id="invite-member-form"
          className="space-y-4"
        >
          {invite.isError && !invite.isPending && (
            <Alert variant="destructive">
              <AlertDescription>
                Could not send the invitation. The address might already
                belong to this organization.
              </AlertDescription>
            </Alert>
          )}

          <div className="space-y-2">
            <Label htmlFor="invite-email">Email</Label>
            <Input
              id="invite-email"
              type="email"
              placeholder="alice@example.com"
              autoComplete="off"
              {...form.register('email')}
            />
            {form.formState.errors.email && (
              <p className="text-xs text-destructive">
                {form.formState.errors.email.message}
              </p>
            )}
          </div>

          <div className="space-y-2">
            <Label htmlFor="invite-role">Role</Label>
            <Controller
              control={form.control}
              name="role"
              render={({ field }) => (
                <Select value={field.value} onValueChange={field.onChange}>
                  <SelectTrigger id="invite-role" className="capitalize">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {ROLES.map((role) => (
                      <SelectItem
                        key={role}
                        value={role}
                        className="capitalize"
                      >
                        {role}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            />
            <p className="text-xs text-muted-foreground">
              Only owners can invite other owners. Admins can invite
              admins and members.
            </p>
          </div>
        </form>

        <DialogFooter>
          <Button
            type="button"
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={invite.isPending}
          >
            Cancel
          </Button>
          <Button
            type="submit"
            form="invite-member-form"
            disabled={invite.isPending}
          >
            {invite.isPending ? 'Sending…' : 'Send invitation'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
