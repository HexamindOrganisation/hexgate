/**
 * Full-screen "presentation" mode for the Playground: its own browser tab with
 * no app chrome (AppShell renders just the route), driven entirely by the URL so
 * it survives a reload of that tab. One source for the flag + the URL, shared by
 * AppShell (hides the sidebar) and the Playground (drops its margins, swaps "open
 * in tab" for a close, and resets the serve session on connect).
 */

import { useSearchParams } from "react-router-dom";

/** The route that opens the Playground full-screen in a new tab. */
export const PRESENT_URL = "/playground?present=1";

/** Whether the current URL is the full-screen presentation view. */
export function usePresentMode(): boolean {
  const [params] = useSearchParams();
  return params.get("present") === "1";
}
