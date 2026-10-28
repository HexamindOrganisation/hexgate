import { afterEach, expect, it, vi } from "vitest";

import {
  clearConsentReturn,
  isConsentPath,
  isLeavableUrl,
  leaveFor,
  pendingConsentReturn,
  stashConsentReturn,
} from "./oauth_consent";

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

it("matches the consent page in any case, with or without a slash", () => {
  expect(isConsentPath("/oauth/consent?state=s")).toBe(true);
  expect(isConsentPath("/oauth/consent/?state=s")).toBe(true);
  expect(isConsentPath("/OAuth/Consent")).toBe(true);
  expect(isConsentPath("/oauth/consentx")).toBe(false);
  expect(isConsentPath("/agents?next=/oauth/consent")).toBe(false);
});

it("never stashes or restores anything but the consent page", () => {
  stashConsentReturn("/agents");
  expect(sessionStorage.length).toBe(0);
  sessionStorage.setItem(
    "hexgate.oauth-consent-return",
    "https://evil.example",
  );
  expect(pendingConsentReturn()).toBeNull();
});

it("treats blocked storage as nothing stashed", () => {
  // Firefox with dom.storage.enabled=false exposes sessionStorage as null.
  vi.stubGlobal("sessionStorage", null);
  expect(pendingConsentReturn()).toBeNull();
  expect(() => stashConsentReturn("/oauth/consent?state=s")).not.toThrow();
  expect(() => clearConsentReturn()).not.toThrow();
});

it("leaves only for http(s) or a reverse-domain app scheme", () => {
  expect(isLeavableUrl("http://127.0.0.1:3000/cb?code=c")).toBe(true);
  expect(isLeavableUrl("https://client.example/cb")).toBe(true);
  expect(isLeavableUrl("com.example.app:/cb")).toBe(true);
  for (const url of [
    "javascript://x/%0aalert(document.domain)//?error=access_denied",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
  ]) {
    expect(isLeavableUrl(url)).toBe(false);
    expect(() => leaveFor(url)).toThrow(/refusing/);
  }
});
