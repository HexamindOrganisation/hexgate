import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { ROLES, type Role } from '@/lib/orgs'

interface RoleSelectProps {
  value: Role
  /** Called with the new role string. The consumer is responsible
   * for the mutation + toast / error handling — this component
   * stays display-only. */
  onChange: (role: Role) => void
  /** Greys out the trigger + makes it un-clickable. Used while the
   * underlying mutation is in flight, or to render in static mode
   * for plain members who can see roles but not change them. */
  disabled?: boolean
}

/**
 * Inline role picker for the members table. Renders as a small
 * native-looking Select trigger so the role badge feels like a
 * dropdown without needing a separate "..." menu.
 *
 * Doesn't enforce the role-escalation matrix client-side — the
 * backend's ``change_member_role`` is the source of truth, and its
 * ``LastOwnerError`` / role-validation errors surface as toasts on
 * the consuming page. Keeping the rule server-side means we don't
 * have to mirror it in the UI when it inevitably gains
 * exceptions (e.g., billing_admin role).
 */
export function RoleSelect({ value, onChange, disabled }: RoleSelectProps) {
  return (
    <Select
      value={value}
      onValueChange={(v) => onChange(v as Role)}
      disabled={disabled}
    >
      <SelectTrigger className="h-7 w-[110px] capitalize">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {ROLES.map((role) => (
          <SelectItem key={role} value={role} className="capitalize">
            {role}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
