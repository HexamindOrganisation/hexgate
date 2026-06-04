import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

interface ConfirmDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void

  /** Title displayed in the header — should be a clear question, e.g.
   * "Leave Acme Inc?" or "Remove alice@example.com?" */
  title: string

  /** One-line description of what happens. Surfaces real consequences
   * (loss of access, mailed cancellation, etc.) so the user isn't
   * confirming blind. */
  description: string

  /** Label for the confirming button — defaults to "Confirm". Set to
   * a verb that matches the action ("Leave organization", "Remove",
   * "Delete") so the user reads the same word they're committing to. */
  confirmLabel?: string

  /** Variant of the confirming button. ``destructive`` is the right
   * default for the actions this dialog covers (leave / remove /
   * cancel) — the red colour reinforces irreversibility. */
  confirmVariant?: 'default' | 'destructive'

  /** Async handler. Awaited before the dialog closes so the button
   * can show its pending state. The handler is responsible for
   * surfacing errors (typically a toast) — this dialog stays focused
   * on the user choosing. */
  onConfirm: () => Promise<void> | void

  /** Pending-state flag from the underlying mutation. When true, the
   * confirm button shows a spinner / "Working…" label and disables. */
  pending?: boolean
}

/**
 * Reusable "are you sure?" modal for destructive actions across the
 * dashboard. Step 2 uses it for "Leave organization"; Step 3 will use
 * it for "Remove member" / "Cancel invitation"; Step 5 for "Rename
 * project" (when the new name collides — confirm overwrite).
 *
 * Kept deliberately simple: title + one-line description + two buttons.
 * Anything richer (custom form, multi-step) lives in a bespoke dialog.
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel = 'Confirm',
  confirmVariant = 'destructive',
  onConfirm,
  pending = false,
}: ConfirmDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>

        <DialogFooter>
          <Button
            type="button"
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={pending}
          >
            Cancel
          </Button>
          <Button
            type="button"
            variant={confirmVariant}
            disabled={pending}
            onClick={() => void onConfirm()}
          >
            {pending ? 'Working…' : confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
