// hourCycle h23, never hour12:false — Chromium renders midnight as "24:mm"
// with the latter. Milliseconds keep calls in the same second apart.
const ms = (d: Date) => "." + String(d.getMilliseconds()).padStart(3, "0");

export const fmtTs = (d: Date) =>
  d.toLocaleString("en-US", {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }) + ms(d);

/** Time of day only, for rows already grouped under a date. */
export const fmtClock = (d: Date) =>
  d.toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }) + ms(d);
