/**
 * TokensPage — the key list's "stale" badge: a key unused for 30+ days, a
 * never-used one counted from its creation.
 */

import { act, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useActive } from "@/lib/active";
import type { TokenListItem } from "@/lib/api";
import { TokensPage } from "@/routes/Tokens";
import { renderWithProviders } from "@/test/render";

const PROJECT = "p1";
const DAY = 86_400_000;
const ago = (days: number) => new Date(Date.now() - days * DAY).toISOString();

const token = (
  name: string,
  created_at: string,
  last_used_at: string | null,
): TokenListItem => ({
  id: name,
  name,
  masked: "fty_live_…abcd",
  scopes: [],
  created_at,
  last_used_at,
  created_by_user_id: null,
  created_by_email: null,
  owner_user_id: null,
  owner_email: null,
});

function stubFetch(tokens: TokenListItem[]) {
  const json = (body: unknown) =>
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  vi.spyOn(window, "fetch").mockImplementation(
    async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      switch (url.pathname) {
        case "/v1/orgs":
          return json([
            {
              id: "org-1",
              slug: "acme",
              name: "Acme",
              created_at: "2026-01-01T00:00:00Z",
              role: "owner",
            },
          ]);
        case "/v1/orgs/org-1/projects":
          return json([
            {
              id: PROJECT,
              org_id: "org-1",
              name: "demo",
              created_at: "2026-01-01T00:00:00Z",
            },
          ]);
        case `/v1/projects/${PROJECT}/tokens`:
          return json(tokens);
        default:
          return new Response("not found", { status: 404 });
      }
    },
  );
}

const rowOf = async (name: string) =>
  (await screen.findByText(name)).closest("tr") as HTMLElement;

describe("TokensPage — key badges", () => {
  beforeEach(() => {
    act(() => {
      useActive.setState({ activeOrgId: "org-1", activeProjectId: PROJECT });
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it("marks only keys unused for 30+ days as stale", async () => {
    stubFetch([
      token("old", ago(90), ago(45)),
      token("abandoned", ago(40), null),
      token("fresh", ago(1), null),
      token("busy", ago(90), ago(1)),
    ]);
    renderWithProviders(<TokensPage />);
    expect(within(await rowOf("old")).getByText("stale")).toBeInTheDocument();
    expect(
      within(await rowOf("abandoned")).getByText("stale"),
    ).toBeInTheDocument();
    expect(within(await rowOf("fresh")).queryByText("stale")).toBeNull();
    expect(within(await rowOf("busy")).queryByText("stale")).toBeNull();
  });
});
