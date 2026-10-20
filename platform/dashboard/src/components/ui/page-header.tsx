import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/** The one page header: title, a one-line description, actions on the right. */
export function PageHeader({
  title,
  description,
  actions,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <header
      className={cn(
        "mb-6 flex flex-wrap items-end justify-between gap-x-6 gap-y-4",
        className,
      )}
    >
      <div className="min-w-0 max-w-[720px]">
        <h1 className="text-title font-semibold tracking-tight">{title}</h1>
        {description && (
          <p className="mt-1.5 text-body text-muted-foreground">
            {description}
          </p>
        )}
      </div>
      {actions && (
        <div className="flex flex-wrap items-center gap-2">{actions}</div>
      )}
    </header>
  );
}
