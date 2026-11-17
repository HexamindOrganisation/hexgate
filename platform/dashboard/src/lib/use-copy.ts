/**
 * Copy text to the clipboard with brief "Copied" feedback. `copied` flips
 * true on success and back after `resetMs` (1200ms: long enough to register,
 * short enough that a second click doesn't see a stale checkmark). A refused
 * clipboard (no permission, insecure origin) leaves it false.
 */

import { useCallback, useEffect, useRef, useState } from "react";

export function useCopy(
  resetMs = 1200,
): [copied: boolean, copy: (text: string) => Promise<void>] {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const copy = useCallback(
    async (text: string) => {
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        return;
      }
      setCopied(true);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), resetMs);
    },
    [resetMs],
  );
  return [copied, copy];
}
