/**
 * Dependency-free chart primitives — stacked bars and a sparkline cover
 * what the dashboard needs without ~80KB of charting
 * library. Series-agnostic: callers describe their stack once as a
 * ``ChartSeries[]`` (see ``components/audit/chart-tokens.ts`` for the
 * outcome stack) and pass per-bucket values keyed by ``series.key``.
 */

import { useState } from "react";

/** One stacked series: colours for SVG strokes/fills plus the Tailwind
 * class used for legend swatches. */
export interface ChartSeries {
  key: string;
  label: string;
  /** CSS colour for SVG stroke/fill attributes. */
  color: string;
  /** Tailwind background class for legend dots (static — the scanner
   * can't see interpolated class names). */
  swatchClass: string;
}

/** One x-axis bucket; ``values`` is keyed by ``ChartSeries.key``. */
export interface ChartBucket {
  label: Date;
  mode: "hour" | "day";
  values: Record<string, number>;
  total: number;
}

// ——— Sparkline (single series area) ———
export function Sparkline({
  data,
  color = "hsl(var(--primary))",
  height = 34,
  width = 150,
}: {
  data: number[];
  color?: string;
  height?: number;
  width?: number;
}) {
  if (!data.length) return null;
  const max = Math.max(...data, 1);
  const pts = data.map((v, i) => {
    const x = (i / Math.max(data.length - 1, 1)) * width;
    const y = height - (v / max) * (height - 3) - 1.5;
    return [x, y] as const;
  });
  const line = pts.map((p) => p.join(",")).join(" ");
  const area = `0,${height} ${line} ${width},${height}`;
  return (
    <svg width={width} height={height} className="block overflow-visible">
      <polygon points={area} fill={color} opacity="0.12" />
      <polyline
        points={line}
        fill="none"
        stroke={color}
        strokeWidth="1.5"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <circle
        cx={pts[pts.length - 1][0]}
        cy={pts[pts.length - 1][1]}
        r="2"
        fill={color}
      />
    </svg>
  );
}

// ——— Stacked bars (one column per bucket) with hover readout ———
/** First series sits at the bottom; the last is drawn on top, so put the
 * series that matters most last (the outcome stack ends with deny). */
export function StackedBars({
  buckets,
  series,
  height = 200,
}: {
  buckets: ChartBucket[];
  series: ChartSeries[];
  height?: number;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const max = Math.max(...buckets.map((b) => b.total), 1);
  const ticks = (() => {
    const n = buckets.length;
    if (n <= 1) return [0];
    const want = Math.min(5, n);
    const step = (n - 1) / (want - 1);
    return [
      ...new Set(Array.from({ length: want }, (_, i) => Math.round(i * step))),
    ];
  })();
  const fmt = (b: ChartBucket) =>
    b.mode === "hour"
      ? b.label
          .toLocaleTimeString("en-US", { hour: "numeric", hour12: true })
          .replace(" ", "")
          .toLowerCase()
      : b.label.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const describe = (b: ChartBucket) =>
    `${fmt(b)}: ${b.total.toLocaleString()} total, ` +
    series.map((s) => `${b.values[s.key] ?? 0} ${s.label}`).join(", ");

  return (
    <div className="w-full">
      <div
        className="relative flex items-end gap-[3px] border-b border-border"
        style={{ height }}
        onMouseLeave={() => setHover(null)}
      >
        <div className="pointer-events-none absolute inset-x-0 top-0 border-t border-dashed border-border" />
        <div className="pointer-events-none absolute inset-x-0 top-1/2 border-t border-dashed border-border" />
        <span className="absolute -top-4 right-0 font-mono text-[11px] text-muted-foreground">
          {max.toLocaleString()}
        </span>
        {buckets.map((b, i) => (
          <div
            key={i}
            role="img"
            aria-label={describe(b)}
            onMouseEnter={() => setHover(i)}
            className="relative flex h-full min-w-0 flex-1 flex-col justify-end"
          >
            <div
              className={`flex flex-col-reverse gap-px overflow-hidden rounded-t-[3px] transition-opacity ${hover !== null && hover !== i ? "opacity-60" : ""}`}
            >
              {series.map((s) => {
                const v = b.values[s.key] ?? 0;
                return v ? (
                  <div
                    key={s.key}
                    style={{
                      height: `${(v / max) * height}px`,
                      background: s.color,
                    }}
                  />
                ) : null;
              })}
            </div>
            {hover === i && (
              <div className="pointer-events-none absolute bottom-full left-1/2 z-10 mb-2 -translate-x-1/2 whitespace-nowrap rounded-lg border border-border bg-popover px-2.5 py-1.5 text-xs shadow-md">
                <div className="font-medium">{fmt(b)}</div>
                {[...series].reverse().map((s) => (
                  <div key={s.key} className="flex items-center gap-1.5">
                    <span className={`size-2 rounded-sm ${s.swatchClass}`} />
                    <span className="text-muted-foreground">{s.label}</span>
                    <span className="ml-auto pl-3 font-mono">
                      {(b.values[s.key] ?? 0).toLocaleString()}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
      <div className="relative mt-2 h-4 font-mono text-[11px] text-muted-foreground">
        {ticks.map((i) => (
          <span
            key={i}
            className="absolute -translate-x-1/2 whitespace-nowrap first:translate-x-0 last:-translate-x-full"
            style={{ left: `${((i + 0.5) / buckets.length) * 100}%` }}
          >
            {fmt(buckets[i])}
          </span>
        ))}
      </div>
    </div>
  );
}
