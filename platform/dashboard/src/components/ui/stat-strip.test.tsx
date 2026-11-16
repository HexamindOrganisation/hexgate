import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { StatStrip } from "./stat-strip";

describe("StatStrip", () => {
  it("makes a stat with onClick a toggle that reports its state", async () => {
    const onClick = vi.fn();
    render(
      <StatStrip
        stats={[
          { key: "total", label: "Decisions", value: "10" },
          { key: "deny", label: "Denied", value: "4", onClick, active: true },
        ]}
      />,
    );
    expect(screen.queryByRole("button", { name: /Decisions/ })).toBeNull();
    const denied = screen.getByRole("button", { name: /Denied/ });
    expect(denied).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(denied);
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("drops empty segments from the split bar", () => {
    const { container } = render(
      <StatStrip
        stats={[{ key: "t", label: "T", value: "1" }]}
        split={[
          { key: "a", value: 0, className: "bg-deny" },
          { key: "b", value: 3, className: "bg-allow-fill" },
        ]}
      />,
    );
    const bar = container.querySelector('[aria-hidden="true"]');
    expect(bar?.children).toHaveLength(1);
  });
});
