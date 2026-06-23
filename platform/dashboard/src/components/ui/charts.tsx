/**
 * Dependency-free SVG chart primitives — an area chart, a donut, and a
 * sparkline cover what the dashboard needs without ~80KB of charting
 * library. Series-agnostic: callers describe their stack once as a
 * ``ChartSeries[]`` (see ``components/audit/chart-tokens.ts`` for the
 * outcome stack) and pass per-bucket values keyed by ``series.key``.
 */

import { useLayoutEffect, useRef, useState } from "react";

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
  /** Stacked-area layer fill opacity (default 0.3). */
  fillOpacity?: number;
}

/** One x-axis bucket; ``values`` is keyed by ``ChartSeries.key``. */
export interface ChartBucket {
  label: Date;
  mode: "hour" | "day";
  values: Record<string, number>;
  total: number;
}

function useMeasure() {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(0);
  useLayoutEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver((ents) => setW(ents[0].contentRect.width));
    ro.observe(ref.current);
    setW(ref.current.clientWidth);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
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

// ——— Stacked area chart with hover tooltip ———
export function AreaChart({
  buckets,
  series,
  height = 240,
}: {
  buckets: ChartBucket[];
  series: ChartSeries[];
  height?: number;
}) {
  const [ref, w] = useMeasure();
  const [hover, setHover] = useState<number | null>(null);
  const padL = 8,
    padR = 8,
    padT = 14,
    padB = 26;
  const W = Math.max(w, 320);
  const innerW = W - padL - padR;
  const innerH = height - padT - padB;
  const maxTotal = Math.max(...buckets.map((d) => d.total), 1);
  const x = (i: number) =>
    padL + (i / Math.max(buckets.length - 1, 1)) * innerW;
  const y = (v: number) => padT + innerH - (v / maxTotal) * innerH;

  const cum = buckets.map(() => 0);
  const layers: {
    key: string;
    color: string;
    fillOpacity: number;
    path: string;
  }[] = [];
  for (const s of series) {
    const top = buckets.map((d, i) => cum[i] + (d.values[s.key] ?? 0));
    const bottom = cum.slice();
    const path =
      top.map((v, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(v)}`).join(" ") +
      " " +
      bottom
        .map(
          (_, i) =>
            `L${x(buckets.length - 1 - i)},${y(bottom[buckets.length - 1 - i])}`,
        )
        .join(" ") +
      " Z";
    layers.push({
      key: s.key,
      color: s.color,
      fillOpacity: s.fillOpacity ?? 0.3,
      path,
    });
    buckets.forEach((_, i) => (cum[i] = top[i]));
  }

  const n = buckets.length;
  const mode = (buckets[0] && buckets[0].mode) || "day";
  const ticks = (() => {
    const want = Math.min(5, n);
    if (n <= 1) return [0];
    const step = (n - 1) / (want - 1);
    const s = new Set<number>();
    for (let i = 0; i < want; i++) s.add(Math.round(i * step));
    return [...s].sort((a, b) => a - b);
  })();
  const fmt = (dt: Date) =>
    mode === "hour"
      ? dt
          .toLocaleTimeString("en-US", { hour: "numeric", hour12: true })
          .replace(" ", "")
          .toLowerCase()
      : dt.toLocaleDateString("en-US", { month: "short", day: "numeric" });

  return (
    <div
      ref={ref}
      className="relative w-full"
      onMouseLeave={() => setHover(null)}
      onMouseMove={(e) => {
        const rect = e.currentTarget.getBoundingClientRect();
        const rx = e.clientX - rect.left - padL;
        const i = Math.round((rx / innerW) * (buckets.length - 1));
        setHover(Math.max(0, Math.min(buckets.length - 1, i)));
      }}
    >
      <svg width={W} height={height} className="block">
        {[0.25, 0.5, 0.75, 1].map((f) => (
          <line
            key={f}
            x1={padL}
            x2={W - padR}
            y1={padT + innerH * (1 - f)}
            y2={padT + innerH * (1 - f)}
            stroke="hsl(var(--border))"
            strokeOpacity="0.5"
            strokeDasharray="2 4"
          />
        ))}
        {layers.map((l) => (
          <path key={l.key} d={l.path} fill={l.color} opacity={l.fillOpacity} />
        ))}
        {(() => {
          const c2 = buckets.map(() => 0);
          return series.map((s) => {
            const top = buckets.map((d, i) => c2[i] + (d.values[s.key] ?? 0));
            const line = top
              .map((v, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(v)}`)
              .join(" ");
            buckets.forEach((_, i) => (c2[i] = top[i]));
            return (
              <path
                key={s.key}
                d={line}
                fill="none"
                stroke={s.color}
                strokeWidth="1.5"
                strokeOpacity="0.9"
              />
            );
          });
        })()}
        {hover != null && (
          <g>
            <line
              x1={x(hover)}
              x2={x(hover)}
              y1={padT}
              y2={padT + innerH}
              stroke="hsl(var(--muted-foreground))"
              strokeOpacity="0.5"
            />
            <circle
              cx={x(hover)}
              cy={y(cum[hover])}
              r="3"
              fill="hsl(var(--primary))"
            />
          </g>
        )}
        {ticks.map((t) => (
          <text
            key={t}
            x={x(t)}
            y={height - 8}
            fontSize="10.5"
            fill="hsl(var(--muted-foreground))"
            textAnchor={t === 0 ? "start" : t === n - 1 ? "end" : "middle"}
            fontFamily="var(--font-mono)"
          >
            {fmt(buckets[t].label)}
          </text>
        ))}
      </svg>
      {hover != null && (
        <div
          className="pointer-events-none absolute top-2 z-[5] min-w-[130px] rounded-lg border border-border bg-popover px-2.5 py-2 text-xs shadow-xl"
          style={{ left: Math.min(Math.max(x(hover) + 10, 8), W - 150) }}
        >
          <div className="mb-1 text-[11px] text-muted-foreground">
            {mode === "hour"
              ? buckets[hover].label.toLocaleTimeString("en-US", {
                  hour: "numeric",
                  minute: "2-digit",
                  hour12: true,
                })
              : buckets[hover].label.toLocaleDateString("en-US", {
                  weekday: "short",
                  month: "short",
                  day: "numeric",
                })}
          </div>
          {series
            .slice()
            .reverse()
            .map((s) => (
              <div
                key={s.key}
                className="flex items-center gap-1.5 leading-[1.7]"
              >
                <span className={`size-2 rounded-sm ${s.swatchClass}`} />
                <span className="flex-1 text-muted-foreground">{s.label}</span>
                <span className="font-mono text-foreground">
                  {buckets[hover].values[s.key] ?? 0}
                </span>
              </div>
            ))}
        </div>
      )}
    </div>
  );
}

// ——— Donut with centred total + per-series legend ———
export function Donut({
  values,
  total,
  series,
  caption,
  size = 168,
  stroke = 20,
}: {
  values: Record<string, number>;
  total: number;
  series: ChartSeries[];
  caption: string;
  size?: number;
  stroke?: number;
}) {
  const r = (size - stroke) / 2;
  const cx = size / 2,
    cy = size / 2;
  const circ = 2 * Math.PI * r;
  const frac = (k: string) => (total ? (values[k] ?? 0) / total : 0);
  // Cumulative offset via prefix sum (no render-time reassignment).
  const segs = series.map((s, i) => ({
    s,
    dash: frac(s.key) * circ,
    offset:
      series.slice(0, i).reduce((sum, ss) => sum + frac(ss.key), 0) * circ,
  }));
  return (
    <div className="flex items-center gap-[18px]">
      <svg width={size} height={size} className="shrink-0 -rotate-90">
        <circle
          cx={cx}
          cy={cy}
          r={r}
          fill="none"
          stroke="hsl(var(--secondary))"
          strokeWidth={stroke}
        />
        {segs.map(({ s, dash, offset }) => (
          <circle
            key={s.key}
            cx={cx}
            cy={cy}
            r={r}
            fill="none"
            stroke={s.color}
            strokeWidth={stroke}
            strokeDasharray={`${dash} ${circ - dash}`}
            strokeDashoffset={-offset}
            strokeLinecap="butt"
          />
        ))}
      </svg>
      <div className="min-w-0 flex-1">
        <div className="text-[26px] font-semibold leading-none tracking-tight">
          {total.toLocaleString()}
        </div>
        <div className="mb-3 text-xs text-muted-foreground">{caption}</div>
        {series.map((s) => {
          const v = values[s.key] ?? 0;
          const pct = total ? Math.round((v / total) * 100) : 0;
          return (
            <div
              key={s.key}
              className="flex items-center gap-2 py-[3px] text-[12.5px]"
            >
              <span className={`size-[9px] rounded-sm ${s.swatchClass}`} />
              <span className="flex-1 text-foreground">{s.label}</span>
              <span className="font-mono text-foreground">
                {v.toLocaleString()}
              </span>
              <span className="w-[34px] text-right font-mono text-muted-foreground">
                {pct}%
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
