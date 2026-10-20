import type { AuditTimeseriesPoint } from "@/lib/api";
import { RANGE_DAYS, type Range } from "@/lib/date-range";

/** Mirror of `bucket_minutes_for_timedelta` in
 * platform/api/hexgate_api/features/audit/service.py — keep in step. */
export function bucketMinutesFor(spanMs: number): number {
  const m = spanMs / 6e4;
  if (m <= 30) return 1;
  if (m <= 60) return 5;
  if (m <= 6 * 60) return 15;
  if (m <= 12 * 60) return 30;
  if (m <= 24 * 60) return 60;
  if (m <= 7 * 24 * 60) return 360;
  return 1440;
}

/** The window the server reads: explicit dates win, else `now - range`. */
export function timeWindow(
  range: Range,
  start: Date | null,
  end: Date | null,
  now = new Date(),
): { from: Date; to: Date } {
  if (start && end) return { from: start, to: end };
  return { from: new Date(+now - RANGE_DAYS[range] * 864e5), to: now };
}

/** The timeseries omits empty buckets; put them back as zeros so a quiet
 * stretch reads as a gap. Buckets are epoch-aligned, like ClickHouse's
 * `toStartOfInterval` on the UTC column. */
export function fillBuckets(
  points: AuditTimeseriesPoint[],
  from: Date,
  to: Date,
  minutes: number,
): AuditTimeseriesPoint[] {
  const step = minutes * 6e4;
  const slot = (ms: number) => Math.floor(ms / step) * step;
  const bySlot = new Map(points.map((p) => [slot(+new Date(p.bucket)), p]));
  const out: AuditTimeseriesPoint[] = [];
  for (let t = slot(+from); t <= +to; t += step) {
    out.push(
      bySlot.get(t) ?? {
        bucket: new Date(t).toISOString(),
        allow: 0,
        deny: 0,
        needs_approval: 0,
      },
    );
  }
  return out;
}
