/**
 * Helpers for the OAuth consent page (``routes/OAuthConsent.tsx``).
 *
 * The page's query string is the MCP client's authorization request, so it
 * must reach the page intact. ProtectedRoute keeps it across a password
 * sign-in in ``state.from``, but Google sign-in leaves the dashboard and that
 * state is lost: the page's address is kept in sessionStorage instead (this
 * tab only) and ProtectedRoute sends the user back to it once signed in.
 */

import { matchPath } from "react-router-dom";

export const CONSENT_PATH = "/oauth/consent";

const RETURN_KEY = "hexgate.oauth-consent-return";

/** True for the consent page, with or without a query, by the router's own
 * matching rules (case, trailing slash), so it agrees with the route. */
export function isConsentPath(path: string): boolean {
  return matchPath(CONSENT_PATH, path.split("?")[0]) !== null;
}

/** Remember ``path`` before leaving for Google; anything but the consent
 * page is ignored, so the restore can never send the user elsewhere. */
export function stashConsentReturn(path: string | undefined): void {
  if (path && isConsentPath(path))
    withStorage((s) => s.setItem(RETURN_KEY, path));
}

/** Read on every signed-in render of every page: it must never throw. */
export function pendingConsentReturn(): string | null {
  const path = withStorage((s) => s.getItem(RETURN_KEY));
  return path && isConsentPath(path) ? path : null;
}

export function clearConsentReturn(): void {
  withStorage((s) => s.removeItem(RETURN_KEY));
}

/** Blocked storage (a browser setting) only loses the stash. */
function withStorage<T>(act: (storage: Storage) => T): T | undefined {
  try {
    return act(sessionStorage);
  } catch {
    return undefined;
  }
}

/** True when a parameter appears twice. ``/authorize`` never sends that,
 * and the server reads the last copy while the page reads the first, so the
 * page could act on a redirect_uri or state the server never checked. */
export function hasRepeatedParams(search: string): boolean {
  const keys = [...new URLSearchParams(search).keys()];
  return new Set(keys).size !== keys.length;
}

export function redirectUri(search: string): string {
  return new URLSearchParams(search).get("redirect_uri") ?? "";
}

/** The client's ``redirect_uri`` carrying ``error=access_denied`` (and its
 * ``state``). Only call it once the server has checked the request: the
 * query alone is whatever the link that opened the page said. */
export function denyRedirect(search: string): string {
  const params = new URLSearchParams(search);
  const url = new URL(params.get("redirect_uri") ?? "");
  url.searchParams.set("error", "access_denied");
  const state = params.get("state");
  if (state !== null) url.searchParams.set("state", state);
  return url.toString();
}

/** http(s), or a private-use scheme in reverse-domain form (RFC 8252,
 * section 7.1: ``com.example.app:``). Never javascript:, data: and the like: the
 * client's own document picks its redirect, and navigating to one of those
 * would run its script as the dashboard. */
export function isLeavableUrl(url: string): boolean {
  try {
    const { protocol } = new URL(url);
    return (
      protocol === "https:" || protocol === "http:" || protocol.includes(".")
    );
  } catch {
    return false;
  }
}

/** Leave the dashboard for the client's redirect: the single exit, which
 * refuses what isLeavableUrl refuses (Approve's redirect_to comes from the
 * server, not the query). ``replace`` so Back can't reopen a spent consent
 * page. Tests stand in for it. */
export function leaveFor(url: string): void {
  if (!isLeavableUrl(url)) throw new Error(`refusing to open ${url}`);
  window.location.replace(url);
}
