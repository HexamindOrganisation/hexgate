import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/** The one page header: title, a one-line description, actions on the right. */
export function PageHeader({
  title,
  description,
  actions,
  align = "end",
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  /** Actions level with the description's last line ("end") or the title. */
  align?: "start" | "end";
  className?: string;
}) {
  return (
    <header
      className={cn(
        "mb-6 flex flex-wrap justify-between gap-x-6 gap-y-4",
        align === "start" ? "items-start" : "items-end",
        className,
      )}
    >
      {/* When there's no room, the actions wrap below and stay right. */}
      <div className="min-w-0 max-w-[720px] flex-1 basis-[420px]">
        <h1 className="text-title font-semibold tracking-tight">{title}</h1>
        {description && (
          <p className="mt-1.5 text-body text-muted-foreground">
            {description}
          </p>
        )}
      </div>
      {actions && (
        <div className="ml-auto flex shrink-0 flex-wrap items-center gap-2">
          {actions}
        </div>
      )}
    </header>
  );
}
