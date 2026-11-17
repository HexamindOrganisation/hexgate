/**
 * QuickstartDialog — it's driven by the `useUi` store, pages through its steps,
 * swaps the register/wrap snippets per framework, and its page actions navigate
 * while closing the dialog (marking the quickstart seen).
 */

import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { QuickstartDialog } from "@/components/quickstart/QuickstartDialog";
import { useUi } from "@/lib/ui";
import { renderWithProviders } from "@/test/render";

function renderDialog() {
  return renderWithProviders(
    <>
      <QuickstartDialog />
      <Routes>
        <Route path="/" element={<div>home page</div>} />
        <Route path="/tokens" element={<div>api keys page</div>} />
      </Routes>
    </>,
  );
}

describe("QuickstartDialog", () => {
  beforeEach(() => {
    window.localStorage.clear();
    useUi.setState({
      quickstartOpen: true,
      quickstartSeen: false,
      quickstartStep: 0,
    });
  });

  it("renders nothing while the store says closed", () => {
    useUi.setState({ quickstartOpen: false });
    renderDialog();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("pages forward and back through the steps", async () => {
    const user = userEvent.setup();
    renderDialog();
    expect(screen.getByText("Step 1 of 6")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /back/i })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: /next/i }));
    expect(screen.getByText("Step 2 of 6")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Mint an API key" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /back/i }));
    expect(screen.getByText("Step 1 of 6")).toBeInTheDocument();
  });

  it("jumps to a step from its dot, and finishes on the last one", async () => {
    const user = userEvent.setup();
    renderDialog();
    await user.click(
      screen.getByRole("button", { name: /step 6: change a rule/i }),
    );
    expect(screen.getByText("Step 6 of 6")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(useUi.getState().quickstartOpen).toBe(false);
    expect(useUi.getState().quickstartSeen).toBe(true);
    // Done finishes the walkthrough: the next open starts from the top.
    expect(useUi.getState().quickstartStep).toBe(0);
  });

  it("swaps the register and wrap snippets per framework", async () => {
    const user = userEvent.setup();
    renderDialog();
    await user.click(
      screen.getByRole("button", { name: /step 3: register and wrap/i }),
    );
    expect(screen.getByText(/wrap_langchain_agent\(agent=graph/)).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Pydantic AI" }));
    expect(screen.getByText(/wrap_pydantic_agent\(agent=agent\)/)).toBeTruthy();
    expect(screen.queryByText(/wrap_langchain_agent\(agent=graph/)).toBeNull();
  });

  it("navigates to the step's page and closes", async () => {
    const user = userEvent.setup();
    renderDialog();
    await user.click(screen.getByRole("button", { name: /next/i }));
    await user.click(screen.getByRole("button", { name: "Mint an API key" }));
    expect(screen.getByText("api keys page")).toBeInTheDocument();
    expect(useUi.getState().quickstartOpen).toBe(false);
    expect(useUi.getState().quickstartSeen).toBe(true);
    // Leaving for a page keeps the step, so reopening resumes there.
    expect(useUi.getState().quickstartStep).toBe(1);
  });

  it("opens at the step the store asks for", () => {
    useUi.setState({ quickstartStep: 4 });
    renderDialog();
    expect(screen.getByText("Step 5 of 6")).toBeInTheDocument();
  });
});
