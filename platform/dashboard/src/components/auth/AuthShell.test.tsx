/**
 * AuthShell — the shared pre-login layout. Renders the mark, title and
 * description, wraps children in a card by default, and shows the footer.
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { AuthShell } from "./AuthShell";

describe("AuthShell", () => {
  it("renders the title, description and footer", () => {
    render(
      <AuthShell
        title="Sign in"
        description="Welcome back"
        footer={<a href="#">Help</a>}
      >
        <button>Go</button>
      </AuthShell>,
    );
    expect(
      screen.getByRole("heading", { name: "Sign in" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Welcome back")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Help" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Go" })).toBeInTheDocument();
  });

  it("wraps children in a card by default, and not when card={false}", () => {
    const { rerender, container } = render(
      <AuthShell title="t">
        <span data-testid="child">x</span>
      </AuthShell>,
    );
    const carded = screen.getByTestId("child").closest(".bg-card");
    expect(carded).not.toBeNull();

    rerender(
      <AuthShell title="t" card={false}>
        <span data-testid="child">x</span>
      </AuthShell>,
    );
    expect(screen.getByTestId("child").closest(".bg-card")).toBeNull();
    expect(container).toBeTruthy();
  });

  it("spins the mark glyph in a working state", () => {
    const { container } = render(
      <AuthShell tone="muted" spin title="Loading…" />,
    );
    expect(container.querySelector(".animate-spin")).not.toBeNull();
  });
});
