import { describe, expect, it } from "vitest";

import { rangeDays } from "./date-range";
import { rangeSearch } from "./range-params";
import {
  EMPTY_USAGE_FILTERS,
  fromUsageSearch,
  usageUrl,
} from "./usage-filters";

describe("usageUrl / fromUsageSearch", () => {
  it("round-trips the agent, model and range", () => {
    const url = usageUrl({
      ...EMPTY_USAGE_FILTERS,
      agent: "support_bot",
      model: "gpt-4o",
      range: "7d",
    });
    const f = fromUsageSearch(new URLSearchParams(url.split("?")[1]));
    expect(f).toMatchObject({
      agent: "support_bot",
      model: "gpt-4o",
      user: "",
      range: "7d",
    });
  });

  it("reads nothing from a bare /usage", () => {
    expect(fromUsageSearch(new URLSearchParams())).toBeNull();
  });
});

describe("rangeSearch", () => {
  it("keeps only the range keys", () => {
    expect(
      rangeSearch(new URLSearchParams("range=7d&agent=x&outcome=deny")),
    ).toBe("?range=7d");
    expect(rangeSearch(new URLSearchParams("agent=x"))).toBe("");
  });
});

describe("rangeDays", () => {
  it("runs an open-ended custom start to now", () => {
    const start = new Date(Date.now() - 30 * 86_400_000);
    expect(rangeDays("24h", start, null)).toBe(30);
  });
});
