import type { ReactNode } from "react";

import { Sparkline } from "@/components/ui/charts";
import { cn } from "@/lib/utils";

export interface Stat {
  key: string;
  label: string;
  value: string;
  sub?: ReactNode;
  /** Tailwind bg class for the swatch before the label (static class). */
  swatchClass?: string;
  /** Tailwind text class for the value (static class). */
  valueClass?: string;
  spark?: number[];
  sparkColor?: string;
  /** Makes the cell a toggle (e.g. filter to this outcome). */
  onClick?: () => void;
  active?: boolean;
}

/** One bordered row of stats, optionally with a 100% split bar under it. */
export function StatStrip({
  stats,
  split,
}: {
  stats: Stat[];
  split?: { key: string; value: number; className: string }[];
}) {
  const splitTotal = split?.reduce((s, x) => s + x.value, 0) ?? 0;
  return (
    <section className="overflow-hidden rounded-xl border border-border bg-card">
      <div className="grid grid-cols-2 md:grid-cols-4 [&>*]:border-border max-md:[&>*:nth-child(n+3)]:border-t md:[&>*+*]:border-l max-md:[&>*:nth-child(even)]:border-l">
        {stats.map((s) => {
          const body = (
            <>
              <span className="flex items-center gap-1.5 text-meta font-medium text-muted-foreground">
                {s.swatchClass && (
                  <span className={cn("size-2 rounded-sm", s.swatchClass)} />
                )}
                {s.label}
              </span>
              <span
                className={cn(
                  "text-[30px] font-semibold leading-none tracking-tight tabular-nums",
                  s.valueClass,
                )}
              >
                {s.value}
              </span>
              <span className="flex items-end justify-between gap-3">
                <span className="text-meta text-muted-foreground">{s.sub}</span>
                {s.spark && s.spark.length > 1 && (
                  <Sparkline
                    data={s.spark}
                    color={s.sparkColor}
                    width={96}
                    height={28}
                  />
                )}
              </span>
            </>
          );
          const cell = "flex flex-col gap-2 px-5 py-4 text-left";
          return s.onClick ? (
            <button
              key={s.key}
              type="button"
              aria-pressed={!!s.active}
              onClick={s.onClick}
              className={cn(
                cell,
                "transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
                s.active && "bg-accent/60",
              )}
            >
              {body}
            </button>
          ) : (
            <div key={s.key} className={cell}>
              {body}
            </div>
          );
        })}
      </div>
      {split && splitTotal > 0 && (
        <div className="flex h-1.5 gap-0.5 mx-5 mb-4" aria-hidden="true">
          {split
            .filter((x) => x.value > 0)
            .map((x) => (
              <span
                key={x.key}
                className={cn("rounded-full", x.className)}
                style={{ flexGrow: x.value }}
              />
            ))}
        </div>
      )}
    </section>
  );
}
