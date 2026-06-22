export const fmtTs = (d: Date) =>
  d.toLocaleString("en-US", {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }) +
  "." +
  String(d.getMilliseconds()).padStart(3, "0");
