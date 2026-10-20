import { describe, expect, it } from "vitest";

import { bucketMinutesFor, fillBuckets, timeWindow } from "./buckets";

const H = 36e5;

describe("bucketMinutesFor", () => {
  it("matches the server's thresholds", () => {
    expect(bucketMinutesFor(30 * 6e4)).toBe(1);
    expect(bucketMinutesFor(H)).toBe(5);
    expect(bucketMinutesFor(6 * H)).toBe(15);
    expect(bucketMinutesFor(12 * H)).toBe(30);
    expect(bucketMinutesFor(24 * H)).toBe(60);
    expect(bucketMinutesFor(7 * 24 * H)).toBe(360);
    expect(bucketMinutesFor(30 * 24 * H)).toBe(1440);
  });
});

describe("timeWindow", () => {
  it("prefers explicit dates over the preset", () => {
    const from = new Date("2026-10-01T00:00:00Z");
    const to = new Date("2026-10-02T00:00:00Z");
    expect(timeWindow("30d", from, to)).toEqual({ from, to });
  });

  it("ends a preset at now", () => {
    const now = new Date("2026-10-20T12:00:00Z");
    expect(timeWindow("7d", null, null, now).from).toEqual(
      new Date("2026-10-13T12:00:00Z"),
    );
  });
});

describe("fillBuckets", () => {
  it("puts empty buckets back between sparse points", () => {
    const out = fillBuckets(
      [
        {
          bucket: "2026-10-01T00:00:00+00:00",
          allow: 3,
          deny: 1,
          needs_approval: 0,
        },
        {
          bucket: "2026-10-04T00:00:00+00:00",
          allow: 2,
          deny: 0,
          needs_approval: 0,
        },
      ],
      new Date("2026-10-01T05:00:00Z"),
      new Date("2026-10-04T05:00:00Z"),
      1440,
    );
    expect(out.map((p) => p.allow)).toEqual([3, 0, 0, 2]);
    expect(new Date(out[1].bucket).toISOString()).toBe(
      "2026-10-02T00:00:00.000Z",
    );
  });
});
