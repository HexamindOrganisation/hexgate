/**
 * The OAuth consent page, and the sign-in round trip that must bring the
 * user back to it with the client's whole query string.
 */

import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Routes } from "react-router-dom";
import { QueryClient } from "@tanstack/react-query";

import { ProtectedRoute } from "@/components/ProtectedRoute";
import { USER_QUERY_KEY } from "@/lib/auth";
import {
  leaveFor,
  pendingConsentReturn,
  stashConsentReturn,
} from "@/lib/oauth_consent";
import { OAuthConsentPage } from "@/routes/OAuthConsent";
import { SignInPage } from "@/routes/SignIn";
import { renderWithProviders } from "@/test/render";

vi.mock("@/lib/oauth_consent", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/oauth_consent")>()),
  leaveFor: vi.fn(),
}));

const QUERY =
  "?response_type=code&client_id=https%3A%2F%2Fclient.example%2Fmeta.json" +
  "&redirect_uri=http%3A%2F%2F127.0.0.1%3A3000%2Fcb&scope=policy%3Aread" +
  "&state=st%26ate&code_challenge=abc&code_challenge_method=S256" +
  "&resource=http%3A%2F%2Flocalhost%3A8080%2Fmcp";

const INFO = {
  client_name: "Claude Code",
  client_host: "client.example",
  redirect_host: "127.0.0.1",
  scopes: [{ name: "policy:read", description: "Read your policies" }],
  projects: [
    { id: "p1", name: "Payments", org_id: "o1", org_name: "Acme" },
    { id: "p2", name: "Search", org_id: "o1", org_name: "Acme" },
  ],
};

// useUser only needs a user; the page never reads its fields.
const ME = { id: "u1", email: "u@example.com" };

type Handler = (init?: RequestInit) => Response;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const SIGNED_IN: Record<string, Handler> = {
  "GET /v1/users/me": () => json(ME),
  "GET /v1/oauth/consent-info": () => json(INFO),
};

/** Routes the fetch spy by ``METHOD path``; the query is ignored. */
function stubFetch(routes: Record<string, Handler> = SIGNED_IN) {
  return vi
    .spyOn(window, "fetch")
    .mockImplementation(async (input, init?: RequestInit) => {
      const url = input.toString();
      const key = `${(init?.method ?? "GET").toUpperCase()} ${url.split("?")[0]}`;
      return routes[key]?.(init) ?? json({ detail: "no stub" }, 404);
    });
}

/** The server was asked about the very query the client sent. */
function expectQueryChecked(fetchSpy: ReturnType<typeof stubFetch>) {
  expect(fetchSpy).toHaveBeenCalledWith(
    `/v1/oauth/consent-info${QUERY}`,
    expect.anything(),
  );
}

/** Neither Approve nor Deny is offered. */
function expectNoChoice() {
  expect(screen.queryByRole("button", { name: "Deny" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
}

/** The app's routes this flow crosses, each behind ProtectedRoute as in App. */
function renderApp(route: string, client?: QueryClient) {
  const guarded = (page: React.ReactNode) => (
    <ProtectedRoute>{page}</ProtectedRoute>
  );
  return renderWithProviders(
    <Routes>
      <Route path="/sign-in" element={<SignInPage />} />
      <Route path="/oauth/consent" element={guarded(<OAuthConsentPage />)} />
      <Route path="/agents" element={guarded(<p>agents page</p>)} />
    </Routes>,
    { initialRoute: route, client },
  );
}

beforeEach(() => {
  sessionStorage.clear();
  vi.mocked(leaveFor).mockClear(); // restoreAllMocks leaves vi.fn alone
});
afterEach(() => vi.restoreAllMocks());

describe("OAuthConsentPage", () => {
  it("shows the client, its hosts, the scopes and the projects", async () => {
    const fetchSpy = stubFetch();
    renderApp(`/oauth/consent${QUERY}`);

    expect(await screen.findByText("Claude Code")).toBeInTheDocument();
    expect(screen.getByText("client.example")).toBeInTheDocument();
    expect(screen.getByText("127.0.0.1")).toBeInTheDocument();
    expect(screen.getByText("Read your policies")).toBeInTheDocument();
    expect(screen.getByLabelText(/Payments/)).not.toBeChecked();
    expect(screen.getByLabelText(/Search/)).toBeInTheDocument();
    expectQueryChecked(fetchSpy);
  });

  it("tells a user with no project to create one first", async () => {
    stubFetch({
      ...SIGNED_IN,
      "GET /v1/oauth/consent-info": () => json({ ...INFO, projects: [] }),
    });
    renderApp(`/oauth/consent${QUERY}`);

    expect(
      await screen.findByText(/Create one in the dashboard/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
  });

  it("keeps Approve disabled until a project is ticked", async () => {
    stubFetch();
    renderApp(`/oauth/consent${QUERY}`);
    const approve = await screen.findByRole("button", { name: "Approve" });
    expect(approve).toBeDisabled();

    await userEvent.click(screen.getByLabelText(/Payments/));
    expect(approve).toBeEnabled();
    await userEvent.click(screen.getByLabelText(/Payments/));
    expect(approve).toBeDisabled();
  });

  it("posts the request with the ticked projects and leaves for redirect_to", async () => {
    const posted: unknown[] = [];
    stubFetch({
      ...SIGNED_IN,
      "POST /v1/oauth/authorize": (init) => {
        posted.push(JSON.parse(String(init?.body)));
        return json({ redirect_to: "http://127.0.0.1:3000/cb?code=c&state=s" });
      },
    });
    renderApp(`/oauth/consent${QUERY}`);
    await userEvent.click(await screen.findByLabelText(/Search/));
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() =>
      expect(leaveFor).toHaveBeenCalledWith(
        "http://127.0.0.1:3000/cb?code=c&state=s",
      ),
    );
    expect(posted).toEqual([
      {
        ...Object.fromEntries(new URLSearchParams(QUERY)),
        projects: ["p2"],
      },
    ]);
  });

  it("sends a user whose session lapsed before Approve to sign in", async () => {
    let expired = false;
    stubFetch({
      "GET /v1/users/me": () => (expired ? json({}, 401) : json(ME)),
      "GET /v1/oauth/consent-info": () => json(INFO),
      "HEAD /v1/auth/google/authorize": () => json({}, 404),
      "POST /v1/oauth/authorize": () => {
        expired = true;
        return json({ detail: "Unauthorized" }, 401);
      },
    });
    renderApp(`/oauth/consent${QUERY}`);
    await userEvent.click(await screen.findByLabelText(/Search/));
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(await screen.findByLabelText("Email")).toBeInTheDocument();
  });

  it("disables Deny while Approve is in flight", async () => {
    stubFetch({
      ...SIGNED_IN,
      "POST /v1/oauth/authorize": () =>
        new Promise<Response>(() => {}) as never,
    });
    renderApp(`/oauth/consent${QUERY}`);
    await userEvent.click(await screen.findByLabelText(/Search/));
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(screen.getByRole("button", { name: "Deny" })).toBeDisabled();
  });

  it("sends access_denied and the state to the redirect_uri on Deny", async () => {
    stubFetch();
    renderApp(`/oauth/consent${QUERY}`);
    await userEvent.click(await screen.findByRole("button", { name: "Deny" }));

    expect(leaveFor).toHaveBeenCalledWith(
      "http://127.0.0.1:3000/cb?error=access_denied&state=st%26ate",
    );
  });

  it("shows the error and no Deny when consent-info refuses the request", async () => {
    stubFetch({
      ...SIGNED_IN,
      "GET /v1/oauth/consent-info": () =>
        json({ detail: "invalid_request: redirect_uri not registered" }, 400),
    });
    renderApp(`/oauth/consent${QUERY}`);

    expect(
      await screen.findByText(/redirect_uri not registered/),
    ).toBeInTheDocument();
    expectNoChoice();
    expect(leaveFor).not.toHaveBeenCalled(); // never to an unchecked URI
  });

  it("offers neither button for a redirect it will not open", async () => {
    stubFetch();
    const query = QUERY.replace(
      "http%3A%2F%2F127.0.0.1%3A3000%2Fcb",
      encodeURIComponent("javascript://x/%0aalert(1)//"),
    );
    renderApp(`/oauth/consent${query}`);

    expect(await screen.findByText(/will not open/)).toBeInTheDocument();
    expectNoChoice();
  });

  it("offers neither button when a parameter is repeated", async () => {
    // The server checks the last copy; the page must not act on the first.
    stubFetch();
    renderApp(
      `/oauth/consent?redirect_uri=https%3A%2F%2Fevil.example%2Fx&${QUERY.slice(1)}`,
    );

    expect(await screen.findByText(/appears twice/)).toBeInTheDocument();
    expectNoChoice();
  });
});

describe("sign-in round trip", () => {
  it("signs a user whose session lapsed back in with the full query", async () => {
    let expired = true;
    const fetchSpy = stubFetch({
      "GET /v1/users/me": () => (expired ? json({}, 401) : json(ME)),
      "GET /v1/oauth/consent-info": () =>
        expired ? json({ detail: "Unauthorized" }, 401) : json(INFO),
      "HEAD /v1/auth/google/authorize": () => json({}, 404),
      "POST /v1/auth/cookie/login": () => {
        expired = false;
        return new Response(null, { status: 204 });
      },
    });
    // The page was open while the cookie expired: useUser's cache says
    // signed in, consent-info says 401.
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Infinity } },
    });
    client.setQueryData(USER_QUERY_KEY, ME);
    renderApp(`/oauth/consent${QUERY}`, client);

    await userEvent.type(await screen.findByLabelText("Email"), ME.email);
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("Claude Code")).toBeInTheDocument();
    expectQueryChecked(fetchSpy);
  });

  it("keeps a fresh consent request over a stale stashed one", async () => {
    stashConsentReturn("/oauth/consent?state=old");
    const fetchSpy = stubFetch();
    renderApp(`/oauth/consent${QUERY}`);

    expect(await screen.findByText("Claude Code")).toBeInTheDocument();
    expectQueryChecked(fetchSpy);
    expect(fetchSpy).not.toHaveBeenCalledWith(
      "/v1/oauth/consent-info?state=old",
      expect.anything(),
    );
  });

  it("brings a signed-out user back with the full query after a password sign-in", async () => {
    let signedIn = false;
    const fetchSpy = stubFetch({
      "GET /v1/users/me": () => (signedIn ? json(ME) : json({}, 401)),
      "HEAD /v1/auth/google/authorize": () => json({}, 404),
      "POST /v1/auth/cookie/login": () => {
        signedIn = true;
        return new Response(null, { status: 204 });
      },
      "GET /v1/oauth/consent-info": () => json(INFO),
    });
    renderApp(`/oauth/consent${QUERY}`);

    await userEvent.type(await screen.findByLabelText("Email"), ME.email);
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText("Claude Code")).toBeInTheDocument();
    expectQueryChecked(fetchSpy);
  });

  it("stashes the consent address before Google sign-in and restores it after", async () => {
    stubFetch({
      "GET /v1/users/me": () => json({}, 401),
      "HEAD /v1/auth/google/authorize": () => json({}),
      "GET /v1/auth/google/authorize": () =>
        json({ authorization_url: "https://accounts.example/auth" }),
    });
    const first = renderApp(`/oauth/consent${QUERY}`);
    await userEvent.click(
      await screen.findByRole("button", { name: "Continue with Google" }),
    );
    expect(pendingConsentReturn()).toBe(`/oauth/consent${QUERY}`);
    first.unmount();

    // Back from Google, signed in, on whatever dashboard page loads first.
    const fetchSpy = stubFetch();
    renderApp("/agents");

    expect(await screen.findByText("Claude Code")).toBeInTheDocument();
    expectQueryChecked(fetchSpy);
    expect(pendingConsentReturn()).toBeNull();
  });
});
