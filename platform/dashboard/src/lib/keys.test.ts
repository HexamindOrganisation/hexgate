import { describe, expect, it } from "vitest";

import type { TokenListItem } from "@/lib/api";
import { isStaleKey } from "./keys";

const NOW = Date.parse("2026-10-20T12:00:00Z");
const key = (created_at: string, last_used_at: string | null) =>
  ({ created_at, last_used_at }) as TokenListItem;

describe("isStaleKey", () => {
  it("flags a key last used over 30 days ago", () => {
    expect(
      isStaleKey(key("2026-01-01T00:00:00Z", "2026-09-01T00:00:00Z"), NOW),
    ).toBe(true);
  });

  it("does not flag a key used recently", () => {
    expect(
      isStaleKey(key("2026-01-01T00:00:00Z", "2026-10-19T00:00:00Z"), NOW),
    ).toBe(false);
  });

  it("counts a never-used key from its creation", () => {
    expect(isStaleKey(key("2026-10-20T11:59:00Z", null), NOW)).toBe(false);
    expect(isStaleKey(key("2026-08-01T00:00:00Z", null), NOW)).toBe(true);
  });
});
