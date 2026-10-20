import { describe, expect, it } from "vitest";

import {
  auditUrl,
  EMPTY_AUDIT_FILTERS,
  fromAuditSearch,
  toAuditSearch,
} from "./audit-filters";

describe("audit filters in the URL", () => {
  it("round-trips every field", () => {
    const f = {
      ...EMPTY_AUDIT_FILTERS,
      range: "7d" as const,
      agent: "support_bot",
      role: "(none)",
      tool: "refund_order",
      user: "u_1",
      outcome: "deny" as const,
      customMode: true,
      start_date: new Date("2026-10-14T00:00:00Z"),
      end_date: new Date("2026-10-15T00:00:00Z"),
    };
    expect(fromAuditSearch(toAuditSearch(f))).toEqual(f);
  });

  it("writes only what is set, range always", () => {
    expect(auditUrl()).toBe("/audit?range=30d");
    expect(auditUrl({ agent: "a", range: "24h" })).toBe(
      "/audit?range=24h&agent=a",
    );
  });

  it("treats a URL without range as carrying no filters", () => {
    expect(fromAuditSearch(new URLSearchParams("agent=a"))).toBeNull();
  });

  it("falls back to defaults for malformed values", () => {
    const f = fromAuditSearch(
      new URLSearchParams("range=1y&outcome=maybe&from=nope&to=nah"),
    )!;
    expect(f.range).toBe("30d");
    expect(f.outcome).toBe("");
    expect(f.start_date).toBeNull();
  });
});
