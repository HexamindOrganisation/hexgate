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
  // Sub-day buckets over several days tick by date; the tooltip always
  // carries the full time.
  const spansDays =
    buckets.length > 1 &&
    +buckets.at(-1)!.label - +buckets[0].label > 30 * 36e5;
  const tick = (b: ChartBucket) =>
    b.mode === "hour" && !spansDays
      ? b.label
          .toLocaleTimeString("en-US", { hour: "numeric", hour12: true })
          .replace(" ", "")
          .toLowerCase()
      : b.label.toLocaleDateString("en-US", {
          month: "short",
          day: "numeric",
          timeZone: b.mode === "day" ? "UTC" : undefined,
        });
  const full = (b: ChartBucket) =>
    b.mode === "hour"
      ? b.label.toLocaleString("en-US", {
          month: "short",
          day: "numeric",
          hour: "numeric",
          minute: "2-digit",
        })
      : // Day buckets start at UTC midnight; label them in UTC so a user
        // west of UTC doesn't see every bar one day early.
        b.label.toLocaleDateString("en-US", {
          weekday: "short",
          month: "short",
          day: "numeric",
          timeZone: "UTC",
        });
  // Each non-zero segment keeps a few pixels, so a handful of denials next
  // to thousands of allows still shows without hovering.
  const MIN_SEGMENT = 3;
  const barHeight = (b: ChartBucket) =>
    Math.max(
      (b.total / max) * height,
      series.filter((s) => (b.values[s.key] ?? 0) > 0).length * MIN_SEGMENT,
    );
  // Keep the readout inside the card at the edges.
  const tipAnchor = (i: number) =>
    i < buckets.length / 4
      ? "left-0"
      : i >= (buckets.length * 3) / 4
        ? "right-0"
        : "left-1/2 -translate-x-1/2";
  const describe = (b: ChartBucket) =>
    `${full(b)}: ${b.total.toLocaleString()} total, ` +
    series.map((s) => `${b.values[s.key] ?? 0} ${s.label}`).join(", ");

  return (
    <div className="w-full">
      <div
        className={`relative flex items-end border-b border-border pt-5 ${buckets.length > 60 ? "gap-px" : "gap-[3px]"}`}
        style={{ height: height + 20 }}
        onMouseLeave={() => setHover(null)}
      >
        <div className="pointer-events-none absolute inset-x-0 top-5 border-t border-dashed border-border" />
        <div
          className="pointer-events-none absolute inset-x-0 border-t border-dashed border-border"
          style={{ top: 20 + height / 2 }}
        />
        <span className="absolute right-0 top-0 font-mono text-[11px] text-muted-foreground">
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
            {/* The bar is sized to the bucket total; segments share it, so
                the tallest bar never passes the top line. */}
            <div
              className={`flex flex-col-reverse overflow-hidden rounded-t-[3px] transition-opacity ${hover !== null && hover !== i ? "opacity-60" : ""}`}
              style={{ height: barHeight(b) }}
            >
              {series.map((s) => {
                const v = b.values[s.key] ?? 0;
                return v ? (
                  <div
                    key={s.key}
                    className="border-t border-card first:border-t-0"
                    style={{
                      flexGrow: v,
                      flexBasis: 0,
                      minHeight: MIN_SEGMENT,
                      background: s.color,
                    }}
                  />
                ) : null;
              })}
            </div>
            {hover === i && (
              <div
                className={`pointer-events-none absolute bottom-full z-10 mb-2 whitespace-nowrap ${tipAnchor(i)}`}
              >
                <div className="rounded-lg border border-border bg-popover px-2.5 py-1.5 text-xs shadow-md">
                  <div className="font-medium">{full(b)}</div>
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
              </div>
            )}
          </div>
        ))}
      </div>
      <div className="relative mt-2 h-4 font-mono text-[11px] text-muted-foreground">
        {ticks.map((i) => (
          <span
            key={i}
            className={
              buckets.length === 1
                ? "absolute left-1/2 -translate-x-1/2 whitespace-nowrap"
                : "absolute -translate-x-1/2 whitespace-nowrap first:translate-x-0 last:-translate-x-full"
            }
            style={
              buckets.length === 1
                ? undefined
                : { left: `${((i + 0.5) / buckets.length) * 100}%` }
            }
          >
            {tick(buckets[i])}
          </span>
        ))}
      </div>
    </div>
  );
}
