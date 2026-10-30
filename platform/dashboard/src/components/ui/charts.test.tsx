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
});
