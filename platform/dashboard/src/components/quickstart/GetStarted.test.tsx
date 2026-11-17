/**
 * GetStarted — Home's setup checklist: hidden until every signal is known and
 * once all are done; ticks off what's done; the key step deep-links into the
 * "Mint API key" window and "Show me" opens the quickstart at its step.
 */

import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { QUICKSTART_STEP } from "@/components/quickstart/content";
import { GetStarted } from "@/components/quickstart/GetStarted";
import { useUi } from "@/lib/ui";
import { renderWithProviders } from "@/test/render";

describe("GetStarted", () => {
  beforeEach(() => {
    useUi.setState({ quickstartOpen: false, quickstartStep: 0 });
  });

  it("renders nothing while a signal is still unknown", () => {
    renderWithProviders(
      <GetStarted hasKey={false} hasAgent={undefined} hasDecision={false} />,
    );
    expect(screen.queryByText(/get started/i)).toBeNull();
  });

  it("renders nothing once every step is done", () => {
    renderWithProviders(<GetStarted hasKey hasAgent hasDecision />);
    expect(screen.queryByText(/get started/i)).toBeNull();
  });

  it("counts what's done and links the key step to the mint window", () => {
    renderWithProviders(
      <GetStarted hasKey={false} hasAgent hasDecision={false} />,
    );
    expect(screen.getByText(/1 of 3 done/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /mint a key/i })).toHaveAttribute(
      "href",
      "/tokens?mint=1",
    );
  });

  it("opens the quickstart at the matching step", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <GetStarted hasKey={false} hasAgent={false} hasDecision={false} />,
    );
    const [, runShowMe] = screen.getAllByRole("button", { name: /show me/i });
    await user.click(runShowMe);
    expect(useUi.getState().quickstartOpen).toBe(true);
    expect(useUi.getState().quickstartStep).toBe(QUICKSTART_STEP.run);

    await user.click(
      screen.getByRole("button", { name: /watch the quickstart/i }),
    );
    expect(useUi.getState().quickstartStep).toBe(QUICKSTART_STEP.idea);
  });
});
