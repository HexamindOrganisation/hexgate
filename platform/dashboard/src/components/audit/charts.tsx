/**
 * Audit-specific chart pieces. The reusable SVG primitives (AreaChart,
 * Donut, Sparkline) live in ``components/ui/charts.tsx``; what stays here
 * is bound to the allow/deny/needs_approval outcome domain.
 */

import { Check, CircleDashed, X } from "lucide-react";
import type { AuditOutcome } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { OUT_LABEL } from "./chart-tokens";

export interface Counts {
  allow: number;
  deny: number;
  needs_approval: number;
  total: number;
}

export interface BreakdownDatum extends Counts {
  key: string;
}

export function DecisionBadge({ d }: { d: AuditOutcome }) {
  const OutcomeIcon = d === "allow" ? Check : d === "deny" ? X : CircleDashed;
  return (
    <Badge variant={d === "needs_approval" ? "approval" : d}>
      <OutcomeIcon className="size-3" strokeWidth={2} />
      {OUT_LABEL[d]}
    </Badge>
  );
}

// ——— Stacked breakdown bar row (allow/approval/deny, deny emphasised) ———
export function BreakdownBar({
  label,
  row,
  max,
  onClick,
  active,
}: {
  label: string;
  row: BreakdownDatum;
  max: number;
  onClick?: () => void;
  active?: boolean;
}) {
  const seg = (k: AuditOutcome) => (row.total ? (row[k] / row.total) * 100 : 0);
  const widthPct = max ? (row.total / max) * 100 : 0;
  return (
    <div
      onClick={onClick}
      className={`mb-[11px] transition-opacity ${onClick ? "cursor-pointer" : ""} ${active === false ? "opacity-45" : ""}`}
    >
      <div className="mb-1 flex justify-between gap-2 text-[12.5px]">
        <span className="truncate font-mono">{label}</span>
        <span className="shrink-0 text-muted-foreground">
          {row.deny > 0 && (
            <span className="mr-2 text-deny">{row.deny} denied</span>
          )}
          <span className="text-foreground">{row.total.toLocaleString()}</span>
        </span>
      </div>
      <div
        className="flex h-1.5 min-w-6 overflow-hidden rounded-[3px] bg-secondary"
        style={{ width: `${Math.max(widthPct, 4)}%` }}
      >
        <div className="bg-allow" style={{ width: `${seg("allow")}%` }} />
        <div
          className="bg-approval"
          style={{ width: `${seg("needs_approval")}%` }}
        />
        <div className="bg-deny" style={{ width: `${seg("deny")}%` }} />
      </div>
    </div>
  );
}
