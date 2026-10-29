/**
 * BreakdownCardShell — the "Show all" overflow control. Caps the ranked list
 * at `collapseAt`, then expands the rest into a scroll area (the pattern the
 * anomalies list uses), so a long "Denials by" never silently drops rows.
 */

import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/test/render";
import { BreakdownCardShell } from "./breakdown-card";

function setup(n: number, collapseAt: number | undefined = 10) {
  const rows = Array.from({ length: n }, (_, i) => ({ key: `tool-${i}` }));
  renderWithProviders(
    <BreakdownCardShell
      dims={[{ id: "tool", label: "Tools" }]}
      dim="tool"
      onDimChange={() => {}}
      metric="volume"
      metricOptions={[{ value: "volume", label: "by volume" }]}
      onMetricChange={() => {}}
      rows={rows}
      renderRow={(row) => <div>{row.key}</div>}
      activeFilterValue=""
      onFilterChange={() => {}}
      emptyMessage="none"
      legend={<span>legend</span>}
      collapseAt={collapseAt}
    />,
  );
}

describe("BreakdownCardShell · show all", () => {
  it("caps at collapseAt, then expands into the full list", async () => {
    setup(12);
    expect(screen.getByText("tool-9")).toBeInTheDocument();
    // Rows past the cap are hidden until expanded.
    expect(screen.queryByText("tool-10")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Show all 12/ }));
    expect(screen.getByText("tool-10")).toBeInTheDocument();
    expect(screen.getByText("tool-11")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Show fewer/ }),
    ).toBeInTheDocument();
  });

  it("shows no toggle when the rows already fit", () => {
    setup(6);
    expect(
      screen.queryByRole("button", { name: /Show all|Show fewer/ }),
    ).toBeNull();
    expect(screen.getByText("tool-5")).toBeInTheDocument();
  });
});
