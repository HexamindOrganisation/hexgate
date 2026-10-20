import { act, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useActive } from "@/lib/active";
import { HomePage } from "@/routes/Home";
import { renderWithProviders } from "@/test/render";

const ORG = {
  id: "o1",
  slug: "o1",
  name: "Org",
  created_at: "",
  role: "owner",
};
const PROJECT = { id: "p1", org_id: "o1", name: "support-bot", created_at: "" };

describe("HomePage", () => {
  beforeEach(() => {
    act(() => useActive.setState({ activeOrgId: "o1", activeProjectId: "p1" }));
    vi.spyOn(window, "fetch").mockImplementation(async (input) => {
      const path = String(input).split("?")[0];
      const body =
        path === "/v1/orgs"
          ? [ORG]
          : path === "/v1/orgs/o1/projects"
            ? [PROJECT]
            : null;
      return new Response(JSON.stringify(body), {
        status: body ? 200 : 404,
        headers: { "Content-Type": "application/json" },
      });
    });
  });

  afterEach(() => vi.restoreAllMocks());

  it("names the project and links to Audit first", async () => {
    renderWithProviders(<HomePage />);
    await waitFor(() =>
      expect(screen.getByText("support-bot")).toBeInTheDocument(),
    );
    const links = screen.getAllByRole("link");
    expect(links.map((l) => l.getAttribute("href"))).toEqual([
      "/audit",
      "/policies",
      "/agents",
    ]);
  });
});
