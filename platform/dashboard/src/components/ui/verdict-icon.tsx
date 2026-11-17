import { Check, CircleDashed, X, type LucideIcon } from "lucide-react";

import type { AuditOutcome, PolicyTestOutcome } from "@/lib/api";
import { cn } from "@/lib/utils";

const ICON: Record<AuditOutcome | PolicyTestOutcome, LucideIcon> = {
  allow: Check,
  deny: X,
  needs_approval: CircleDashed,
  approval_required: CircleDashed,
};

/** The shape cue every allow / deny / approval mark carries alongside its
 * colour: allow (warm green) and deny (orange-red) sit close for red-green
 * colour blindness, so the check and the cross tell them apart. Takes both
 * the audit (`needs_approval`) and policy (`approval_required`) spellings. */
export function VerdictIcon({
  verdict,
  className,
  style,
}: {
  verdict: AuditOutcome | PolicyTestOutcome;
  className?: string;
  style?: React.CSSProperties;
}) {
  const Icon = ICON[verdict];
  return (
    <Icon
      aria-hidden
      className={cn("size-3 shrink-0", className)}
      style={style}
      strokeWidth={2.25}
    />
  );
}
