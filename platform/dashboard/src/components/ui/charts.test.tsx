import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StackedBars } from "./charts";

const SERIES = [
  { key: "allow", label: "allow", color: "gray", swatchClass: "bg-allow-fill" },
  { key: "deny", label: "deny", color: "red", swatchClass: "bg-deny" },
];

describe("StackedBars", () => {
  it("labels each bucket with its counts", () => {
    render(
      <StackedBars
        series={SERIES}
        buckets={[
          {
            label: new Date(Date.UTC(2026, 9, 14)),
            mode: "day",
            values: { allow: 46, deny: 22 },
            total: 68,
          },
        ]}
      />,
    );
    expect(
      screen.getByRole("img", {
        name: "Wed, Oct 14: 68 total, 46 allow, 22 deny",
      }),
    ).toBeInTheDocument();
  });

  it("names sub-day buckets with their time", () => {
    render(
      <StackedBars
        series={SERIES}
        buckets={[0, 6].map((h) => ({
          label: new Date(2026, 9, 14, h),
          mode: "hour" as const,
          values: { allow: 1, deny: 0 },
          total: 1,
        }))}
      />,
    );
    expect(
      screen.getByRole("img", { name: /^Oct 14, 6:00 AM: 1 total/ }),
    ).toBeInTheDocument();
  });

  it("tells sub-hour ticks apart by their minutes", () => {
    const { container } = render(
      <StackedBars
        series={SERIES}
        buckets={Array.from({ length: 9 }, (_, i) => ({
          label: new Date(2026, 9, 14, 0, i * 15),
          mode: "hour" as const,
          values: { allow: 1, deny: 0 },
          total: 1,
        }))}
      />,
    );
    const ticks = tickTexts(container);
    expect(ticks).toContain("1:00am");
    expect(new Set(ticks).size).toBe(ticks.length);
  });

  it("keeps a 24h chart's ticks to the hour", () => {
    const { container } = render(
      <StackedBars
        series={SERIES}
        bucketMinutes={60}
        buckets={Array.from({ length: 25 }, (_, i) => ({
          label: new Date(2026, 9, 1, 12 + i),
          mode: "hour" as const,
          values: { allow: 1, deny: 0 },
          total: 1,
        }))}
      />,
    );
    expect(tickTexts(container)[0]).toBe("12pm");
  });

  it("dates sub-day ticks when the chart spans days", () => {
    const { container } = render(
      <StackedBars
        series={SERIES}
        buckets={Array.from({ length: 9 }, (_, i) => ({
          label: new Date(2026, 9, 1, i * 6),
          mode: "hour" as const,
          values: { allow: 1, deny: 0 },
          total: 1,
        }))}
      />,
    );
    const ticks = tickTexts(container);
    expect(ticks[0]).toBe("Oct 1, 12am");
    expect(new Set(ticks).size).toBe(ticks.length);
  });
});

/** The x-axis labels, left to right. */
const tickTexts = (root: HTMLElement) =>
  [...root.querySelectorAll("div.h-4 > span")].map((s) => s.textContent ?? "");
