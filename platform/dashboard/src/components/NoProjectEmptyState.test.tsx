/**
 * Smoke tests for NoProjectEmptyState.
 *
 * Two assertions worth pinning:
 *
 *   1. Renders the explanatory copy with the resource name interpolated.
 *   2. The CTA opens CreateProjectDialog (so the user has an obvious
 *      way out of "no projects yet" without diving into the org
 *      switcher dropdown).
 */

import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NoProjectEmptyState } from "@/components/NoProjectEmptyState";
import { renderWithProviders } from "@/test/render";

describe("NoProjectEmptyState", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("interpolates the resource name into the help copy", () => {
    renderWithProviders(<NoProjectEmptyState resource="tokens" />);
    expect(screen.getByText(/No project selected/i)).toBeInTheDocument();
    // Mentions the resource so the user knows which page they're on.
    expect(screen.getByText(/organization's tokens/i)).toBeInTheDocument();
  });

  it("'Create project' button opens the CreateProjectDialog", async () => {
    const user = userEvent.setup();
    renderWithProviders(<NoProjectEmptyState resource="agents" />);

    // Dialog isn't mounted until the button is clicked. The dialog
    // title duplicates the button label, so we check the dialog's
    // unique description copy instead.
    expect(screen.queryByText(/Projects hold agents/i)).not.toBeInTheDocument();

    await user.click(screen.getByText("Create project"));

    expect(
      await screen.findByText(/Projects hold agents/i),
    ).toBeInTheDocument();
  });
});
