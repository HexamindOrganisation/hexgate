import { endOfDay, startOfDay } from "date-fns";
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
      start_date: startOfDay(new Date(2026, 9, 14)),
      end_date: endOfDay(new Date(2026, 9, 15)),
    };
    expect(fromAuditSearch(toAuditSearch(f))).toEqual(f);
  });

  it("writes only what is set, range always", () => {
    expect(auditUrl()).toBe("/audit?range=30d");
    expect(auditUrl({ agent: "a", range: "24h" })).toBe(
      "/audit?range=24h&agent=a",
    );
  });

  it("treats a bare URL as carrying no filters", () => {
    expect(fromAuditSearch(new URLSearchParams(""))).toBeNull();
  });

  it("honours a hand-written link without range", () => {
    const f = fromAuditSearch(new URLSearchParams("agent=billing"))!;
    expect(f.agent).toBe("billing");
    expect(f.range).toBe("30d");
  });

  it("keeps a custom range that has only a start day", () => {
    const f = fromAuditSearch(
      toAuditSearch({
        customMode: true,
        start_date: startOfDay(new Date(2026, 9, 14)),
      }),
    )!;
    expect(f.customMode).toBe(true);
    expect(f.start_date).toEqual(startOfDay(new Date(2026, 9, 14)));
    expect(f.end_date).toBeNull();
  });

  it("writes custom days as calendar dates", () => {
    expect(
      toAuditSearch({ start_date: new Date(2026, 9, 14, 23, 30) }).get("from"),
    ).toBe("2026-10-14");
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
