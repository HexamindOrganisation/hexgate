import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

const TONES = {
  attention: {
    box: "border-approval/35 bg-approval/10",
    icon: "bg-approval/20 text-approval",
  },
  info: { box: "border-border bg-muted/40", icon: "bg-muted text-foreground" },
} as const;

/** A highlighted strip for something that needs a look (anomaly, warning). */
export function Callout({
  tone = "attention",
  icon,
  title,
  children,
  actions,
  className,
}: {
  tone?: keyof typeof TONES;
  icon?: ReactNode;
  title: ReactNode;
  children?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  const t = TONES[tone];
  return (
    <section
      className={cn(
        "flex flex-wrap items-center gap-x-4 gap-y-3 rounded-xl border px-4 py-3.5",
        t.box,
        className,
      )}
    >
      {icon && (
        <span
          className={cn(
            "grid size-9 shrink-0 place-items-center rounded-lg [&_svg]:size-4",
            t.icon,
          )}
        >
          {icon}
        </span>
      )}
      <div className="min-w-0 flex-1 basis-[320px]">
        <div className="text-sm font-semibold">{title}</div>
        {children && (
          <div className="mt-0.5 text-meta text-muted-foreground">
            {children}
          </div>
        )}
      </div>
      {actions && (
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {actions}
        </div>
      )}
    </section>
  );
}
