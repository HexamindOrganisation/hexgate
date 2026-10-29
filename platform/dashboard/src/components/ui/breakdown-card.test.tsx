/**
 * BreakdownCardShell — the "Show all" overflow control. Caps the ranked list
 * at `collapseAt`, then expands the rest into a scroll area (the pattern the
 * anomalies list uses), so a long "Denials by" never silently drops rows.
 */

import { useState } from "react";
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/test/render";
import { BreakdownCardShell } from "./breakdown-card";

function shell(dim: string, n: number, collapseAt: number | undefined = 10) {
  const rows = Array.from({ length: n }, (_, i) => ({ key: `${dim}-${i}` }));
  return (
    <BreakdownCardShell
      dims={[
        { id: "tool", label: "Tools" },
        { id: "role", label: "Roles" },
      ]}
      dim={dim}
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
    />
  );
}

function setup(n: number, collapseAt: number | undefined = 10) {
  renderWithProviders(shell("tool", n, collapseAt));
}

/** Keeps the shell mounted while a control flips a prop, so prop-change
 * behaviour (not a remount) is what the test exercises. */
function Harness({ vary }: { vary: "dim" | "metric" }) {
  const [dim, setDim] = useState("tool");
  const [metric, setMetric] = useState("volume");
  const rows = Array.from({ length: 12 }, (_, i) => ({ key: `${dim}-${i}` }));
  return (
    <>
      <button
        onClick={() => (vary === "dim" ? setDim("role") : setMetric("denials"))}
      >
        flip
      </button>
      <BreakdownCardShell
        dims={[
          { id: "tool", label: "Tools" },
          { id: "role", label: "Roles" },
        ]}
        dim={dim}
        onDimChange={() => {}}
        metric={metric}
        metricOptions={[
          { value: "volume", label: "by volume" },
          { value: "denials", label: "by denials" },
        ]}
        onMetricChange={() => {}}
        rows={rows}
        renderRow={(row) => <div>{row.key}</div>}
        activeFilterValue=""
        onFilterChange={() => {}}
        emptyMessage="none"
        legend={<span>legend</span>}
        collapseAt={10}
      />
    </>
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

  it("collapses again when the dimension changes (same mount)", async () => {
    renderWithProviders(<Harness vary="dim" />);
    await userEvent.click(screen.getByRole("button", { name: /Show all 12/ }));
    expect(screen.getByText("tool-11")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "flip" }));
    expect(
      screen.getByRole("button", { name: /Show all 12/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText("role-11")).not.toBeInTheDocument();
  });

  it("keeps the expansion when only the metric changes", async () => {
    renderWithProviders(<Harness vary="metric" />);
    await userEvent.click(screen.getByRole("button", { name: /Show all 12/ }));
    expect(screen.getByText("tool-11")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "flip" }));
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
