import { useEffect, useState } from "react";

/** The current time, re-read every `ms`, so "5m ago" keeps counting even
 * when no new data arrives to re-render it. */
export function useNow(ms: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}
