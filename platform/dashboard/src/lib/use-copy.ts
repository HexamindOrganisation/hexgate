/**
 * Copy text to the clipboard with brief feedback. `status` is "copied" on
 * success or "failed" when the clipboard refused (no permission, insecure
 * origin), and goes back to null after `resetMs` (1200ms: long enough to
 * register, short enough that a second click doesn't see a stale result).
 * Callers copying something that can't be shown again should say so on
 * "failed".
 */

import { useCallback, useEffect, useRef, useState } from "react";

export type CopyStatus = "copied" | "failed" | null;

export function useCopy(
  resetMs = 1200,
): [status: CopyStatus, copy: (text: string) => Promise<void>] {
  const [status, setStatus] = useState<CopyStatus>(null);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const copy = useCallback(
    async (text: string) => {
      let next: CopyStatus = "copied";
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        next = "failed";
      }
      setStatus(next);
      window.clearTimeout(timer.current);
      // A failure stays up a little longer: it's the one worth reading.
      timer.current = window.setTimeout(
        () => setStatus(null),
        next === "failed" ? resetMs * 4 : resetMs,
      );
    },
    [resetMs],
  );
  return [status, copy];
}
