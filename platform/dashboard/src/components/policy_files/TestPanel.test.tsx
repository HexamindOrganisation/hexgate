/**
 * TestPanel tests — the decision tester drives the role/agent form and posts
 * the default sample call to `/policy/test`, rendering the verdict (with its
 * reason, violations, and hint). Covers the allow path, an error path, and the
 * two disabled states (no roles / doesn't resolve).
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { PolicyTestResponse } from "@/lib/api";
import { TestPanel } from "./TestPanel";
import { renderWithProviders } from "@/test/render";

function stubTest(body: unknown, status = 200): ReturnType<typeof vi.fn> {
  const spy = vi.fn();
  vi.spyOn(window, "fetch").mockImplementation(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      spy(String(input), init);
      return new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    },
  );
  return spy;
}

function renderPanel(props: Partial<Parameters<typeof TestPanel>[0]> = {}) {
  return renderWithProviders(
    <TestPanel
      projectId="p1"
      roleNames={["support", "billing"]}
      draft={null}
      resolves
      {...props}
    />,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("TestPanel", () => {
  it("posts the sample call and renders an ALLOW verdict", async () => {
    const verdict: PolicyTestResponse = {
      outcome: "allow",
      reason: "within refund limit",
      violations: [],
      hint: "looks good",
    };
    const spy = stubTest(verdict);
    renderPanel();

    await userEvent.click(screen.getByRole("button", { name: /check/i }));

    await waitFor(() => expect(screen.getByText("ALLOW")).toBeInTheDocument());
    expect(screen.getByText("within refund limit")).toBeInTheDocument();
    expect(screen.getByText("looks good")).toBeInTheDocument();
    const [url, init] = spy.mock.calls[0];
    expect(url).toContain("/v1/projects/p1/policy/test");
    expect(init?.method).toBe("POST");
    const sent = JSON.parse(init.body as string);
    expect(sent).toMatchObject({ role: "support", tool: "refund_order" });
  });

  it("renders a DENY verdict with its violations", async () => {
    stubTest({
      outcome: "deny",
      reason: "over the cap",
      violations: ["args.amount <= 100"],
      hint: null,
    });
    renderPanel();
    await userEvent.click(screen.getByRole("button", { name: /check/i }));
    await waitFor(() => expect(screen.getByText("DENY")).toBeInTheDocument());
    expect(screen.getByText(/args\.amount <= 100/)).toBeInTheDocument();
  });

  it("shows an error when the evaluation fails", async () => {
    stubTest({ detail: "could not resolve policy" }, 422);
    renderPanel();
    await userEvent.click(screen.getByRole("button", { name: /check/i }));
    await waitFor(() =>
      expect(screen.getByText("could not resolve policy")).toBeInTheDocument(),
    );
  });

  it("selects a different role before testing", async () => {
    const spy = stubTest({
      outcome: "allow",
      reason: null,
      violations: [],
      hint: null,
    });
    renderPanel();
    await userEvent.selectOptions(screen.getByRole("combobox"), "billing");
    await userEvent.click(screen.getByRole("button", { name: /check/i }));
    await waitFor(() => expect(spy).toHaveBeenCalled());
    const sent = JSON.parse(spy.mock.calls[0][1].body as string);
    expect(sent.role).toBe("billing");
  });

  it("is disabled and prompts to declare a role when none exist", () => {
    renderPanel({ roleNames: [] });
    expect(screen.getByText(/Declare a role/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /check/i })).toBeDisabled();
  });

  it("is disabled with a doesn't-resolve message", () => {
    renderPanel({ resolves: false });
    expect(screen.getByText(/doesn't currently resolve/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /check/i })).toBeDisabled();
  });
});
