import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { RolePickerDialog } from "./RolePicker";
import { renderWithProviders } from "@/test/render";

describe("RolePickerDialog", () => {
  it("lists every role when none runs the agent", () => {
    renderWithProviders(
      <RolePickerDialog
        options={[
          { name: "ops", runs: false },
          { name: "guest", runs: false },
        ]}
        picked={null}
        onClose={() => undefined}
        onApply={() => undefined}
      />,
    );
    expect(screen.getByRole("button", { name: /^ops/ })).toBeInTheDocument();
    expect(screen.queryByText(/No role matches/)).toBeNull();
  });

  it("picks one role, or every role", async () => {
    const onApply = vi.fn();
    renderWithProviders(
      <RolePickerDialog
        options={[
          { name: "ops", runs: true },
          { name: "guest", runs: false },
        ]}
        picked="ops"
        onClose={() => undefined}
        onApply={onApply}
      />,
    );
    // Only roles that run it, by default: guest is hidden and said so.
    expect(screen.queryByRole("button", { name: /^guest/ })).toBeNull();
    expect(screen.getByText(/1 more role hidden/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Every role/ }));
    expect(onApply).toHaveBeenCalledWith(null);
  });
});
