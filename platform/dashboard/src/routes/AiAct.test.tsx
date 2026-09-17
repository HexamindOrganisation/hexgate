/**
 * Tests for the /ai-act page.
 *
 * Invariants:
 *   1. no-project scope → the empty state, no AI Act fetch.
 *   2. The inventory lists every agent and names what each entry is missing.
 *   3. A complete entry reads as complete.
 *   4. Recording an entry PUTs exactly the operator's assertion.
 *   5. A tier other than high-risk stores no Annex III point.
 *   6. Generate posts the selected period and refreshes the history.
 *   7. Downloads fetch the annex and save it.
 *   8. A failed load never renders as an empty or incomplete result.
 *   9. No copy on the page — dialog included — claims conformity.
 */

import {
  act,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentClassificationRead, AiActReport } from "@/lib/api";
import { useActive } from "@/lib/active";
import { AiActPage } from "@/routes/AiAct";
import { renderWithProviders } from "@/test/render";

const PROJECT = "p1";

/** The scope disclaimer, verbatim. The copy guard below strips it before
 * policing conformity language, so the sentence that denies conformity is the
 * only place the word may appear. */
const DISCLAIMER =
  "it does not assert that any system, or its operator, is in conformity with that Regulation";

interface Call {
  url: string;
  method: string;
  body?: unknown;
}

const EMPTY_ENTRY: AgentClassificationRead = {
  intended_purpose: null,
  operator_role: null,
  risk_tier: null,
  annex_iii_point: null,
  oversight_owner_name: null,
  oversight_owner_contact: null,
  checker_last_update_date: null,
  recorded_by_user_id: null,
  recorded_at: null,
};

const COMPLETE_ENTRY: AgentClassificationRead = {
  intended_purpose: "Prepares creditworthiness assessments.",
  operator_role: "deployer",
  risk_tier: "high_risk",
  annex_iii_point: "5(b)",
  oversight_owner_name: "C. Martin",
  oversight_owner_contact: "c.martin@acme.example",
  checker_last_update_date: "2026-05-14",
  recorded_by_user_id: "usr_1",
  recorded_at: "2026-06-02T09:00:00Z",
};

const REPORT: AiActReport = {
  id: "rpt_1",
  project_id: PROJECT,
  period_start: "2026-03-15T00:00:00Z",
  period_end: "2026-09-11T00:00:00Z",
  generated_at: "2026-09-11T14:02:17Z",
  generated_by_user_id: "usr_1",
  generated_by_email: "c.martin@acme.example",
  annex_sha256: "7c1e9b04aa11bb22cc33dd44",
  signing_kid: "hexgate-root-2026-01",
};

interface StubOptions {
  agents?: { name: string }[];
  classifications?: Record<string, AgentClassificationRead>;
  reports?: AiActReport[];
  /** Agent names whose classification GET fails with this status. */
  classificationStatus?: Record<string, number>;
  /** Status for GET .../ai-act/reports; 200 unless set. */
  reportsStatus?: number;
}

function stubFetch({
  agents = [],
  classifications = {},
  reports = [],
  classificationStatus = {},
  reportsStatus = 200,
}: StubOptions = {}): Call[] {
  const calls: Call[] = [];
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });

  vi.spyOn(window, "fetch").mockImplementation(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const raw = typeof input === "string" ? input : input.toString();
      const url = new URL(raw, "http://localhost");
      const method = init?.method ?? "GET";
      calls.push({
        url: url.pathname + url.search,
        method,
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      });

      const base = `/v1/projects/${PROJECT}`;
      const classification = url.pathname.match(
        /^\/v1\/projects\/[^/]+\/agents\/([^/]+)\/classification$/,
      );

      switch (true) {
        case url.pathname === "/v1/orgs":
          return json([
            {
              id: "org-1",
              slug: "acme",
              name: "Acme Inc",
              created_at: "2026-01-01T00:00:00Z",
              role: "owner",
            },
          ]);
        case url.pathname === "/v1/orgs/org-1/projects":
          return json([
            {
              id: PROJECT,
              org_id: "org-1",
              name: "demo-project",
              created_at: "2026-01-01T00:00:00Z",
            },
          ]);
        case !!classification && method === "PUT":
          return json({
            ...EMPTY_ENTRY,
            ...(init?.body ? JSON.parse(String(init.body)) : {}),
            recorded_by_user_id: "usr_1",
            recorded_at: "2026-09-17T10:00:00Z",
          });
        case !!classification: {
          const name = (classification as RegExpMatchArray)[1];
          const status = classificationStatus[name];
          if (status) return json({ detail: "nope" }, status);
          return json(classifications[name] ?? EMPTY_ENTRY);
        }
        case url.pathname === `${base}/agents`:
          return json(agents);
        case url.pathname === `${base}/ai-act/report` && method === "POST":
          return json(REPORT, 201);
        case url.pathname === `${base}/ai-act/reports`:
          return reportsStatus === 200
            ? json(reports)
            : json({ detail: "nope" }, reportsStatus);
        case url.pathname.endsWith("/annex"):
          return new Response('{"annex": true}', {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        default:
          return new Response("not found", { status: 404 });
      }
    },
  );
  return calls;
}

describe("AiActPage", () => {
  beforeEach(() => {
    act(() => {
      useActive.setState({ activeOrgId: "org-1", activeProjectId: PROJECT });
    });
    // jsdom implements neither, and `saveBlob` calls both. Assigning rather
    // than stubbing the whole `URL` global keeps the `new URL(...)` the fetch
    // stub does working.
    URL.createObjectURL = vi.fn(() => "blob:annex");
    URL.revokeObjectURL = vi.fn();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the no-project empty state and skips the AI Act fetches", async () => {
    act(() => {
      useActive.setState({ activeOrgId: "org-1", activeProjectId: null });
    });
    const calls = stubFetch();
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    expect(await screen.findByText("No project selected")).toBeInTheDocument();
    expect(calls.some((c) => c.url.includes("/ai-act"))).toBe(false);
  });

  it("lists every agent and names the fields an entry is missing", async () => {
    stubFetch({ agents: [{ name: "devops_agent" }] });
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    expect(await screen.findByText("devops_agent")).toBeInTheDocument();
    expect(screen.getByText("Incomplete")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Missing: Intended purpose, Operator role, Risk tier, Human-oversight owner",
      ),
    ).toBeInTheDocument();
  });

  it("reads a fully recorded entry as complete", async () => {
    stubFetch({
      agents: [{ name: "credit_review_agent" }],
      classifications: { credit_review_agent: COMPLETE_ENTRY },
    });
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    expect(await screen.findByText("Complete")).toBeInTheDocument();
    expect(screen.getByText("High-risk")).toBeInTheDocument();
    expect(screen.getByText("5(b)")).toBeInTheDocument();
    expect(screen.getByText("C. Martin")).toBeInTheDocument();
    expect(screen.queryByText(/^Missing:/)).toBeNull();
  });

  it("treats a prefilled-but-unsaved entry as incomplete", async () => {
    // The GET prefills intended_purpose from the manifest; until the operator
    // PUTs it, it's Hexgate's guess rather than their assertion.
    stubFetch({
      agents: [{ name: "support_agent" }],
      classifications: {
        support_agent: { ...COMPLETE_ENTRY, recorded_at: null },
      },
    });
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    expect(await screen.findByText("Incomplete")).toBeInTheDocument();
    expect(screen.queryByText(/^Missing:/)).toBeNull();
  });

  it("PUTs the operator's assertion when an entry is recorded", async () => {
    const calls = stubFetch({ agents: [{ name: "devops_agent" }] });
    const user = userEvent.setup();
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    await user.click(
      await screen.findByRole("button", { name: /record entry/i }),
    );
    const dialog = await screen.findByRole("dialog");

    await user.type(
      within(dialog).getByLabelText("Intended purpose"),
      "Rotates deploy credentials.",
    );
    await user.type(
      within(dialog).getByLabelText("Human-oversight owner"),
      "L. Okafor",
    );
    await user.click(
      within(dialog).getByRole("button", { name: /save entry/i }),
    );

    await waitFor(() => {
      const put = calls.find((c) => c.method === "PUT");
      expect(put?.url).toBe(
        `/v1/projects/${PROJECT}/agents/devops_agent/classification`,
      );
      expect(put?.body).toEqual({
        intended_purpose: "Rotates deploy credentials.",
        operator_role: null,
        risk_tier: null,
        annex_iii_point: null,
        oversight_owner_name: "L. Okafor",
        oversight_owner_contact: null,
        checker_last_update_date: null,
      });
    });
  });

  it("stores no Annex III point when the tier is not high-risk", async () => {
    // The stored 5(b) belongs to the high-risk assertion it was recorded
    // under; downgrading the tier must not carry it along.
    const calls = stubFetch({
      agents: [{ name: "credit_review_agent" }],
      classifications: { credit_review_agent: COMPLETE_ENTRY },
    });
    const user = userEvent.setup();
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    await user.click(
      await screen.findByRole("button", { name: /edit entry/i }),
    );
    const dialog = await screen.findByRole("dialog");

    await user.click(within(dialog).getByLabelText("Risk tier"));
    await user.click(await screen.findByRole("option", { name: "Minimal" }));
    // The Annex III field belongs to a high-risk entry only.
    expect(within(dialog).queryByLabelText("Annex III point")).toBeNull();

    await user.click(
      within(dialog).getByRole("button", { name: /save entry/i }),
    );

    await waitFor(() => {
      const put = calls.find((c) => c.method === "PUT");
      expect(put?.body).toMatchObject({
        risk_tier: "minimal",
        annex_iii_point: null,
      });
    });
  });

  it("generates a report over the selected period and refetches the history", async () => {
    const calls = stubFetch({ reports: [] });
    const user = userEvent.setup();
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    // `<input type="date">` takes a whole value, not keystrokes.
    const from = await screen.findByLabelText("From (UTC)");
    fireEvent.change(from, { target: { value: "2026-03-15" } });
    fireEvent.change(screen.getByLabelText("To (UTC)"), {
      target: { value: "2026-09-11" },
    });

    await user.click(screen.getByRole("button", { name: /generate report/i }));

    await waitFor(() => {
      const post = calls.find(
        (c) =>
          c.method === "POST" &&
          c.url === `/v1/projects/${PROJECT}/ai-act/report`,
      );
      expect(post?.body).toEqual({
        from: "2026-03-15T00:00:00.000Z",
        to: "2026-09-11T23:59:59.999Z",
      });
    });
    // The new report lands in the history list via an invalidation, not a
    // local append — the server owns the id, digest and provenance.
    await waitFor(() =>
      expect(
        calls.filter((c) => c.url === `/v1/projects/${PROJECT}/ai-act/reports`)
          .length,
      ).toBeGreaterThan(1),
    );
  });

  it("refuses to generate when the period ends before it starts", async () => {
    const calls = stubFetch();
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    const from = await screen.findByLabelText("From (UTC)");
    fireEvent.change(from, { target: { value: "2026-12-31" } });

    expect(
      screen.getByRole("button", { name: /generate report/i }),
    ).toBeDisabled();
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("downloads the annex of a past report", async () => {
    const calls = stubFetch({ reports: [REPORT] });
    const user = userEvent.setup();
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    await user.click(await screen.findByRole("button", { name: /annex/i }));

    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.url ===
            `/v1/projects/${PROJECT}/ai-act/reports/${REPORT.id}/annex`,
        ),
      ).toBe(true),
    );
    await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalled());
  });

  it("shows the history row's period, author and digest", async () => {
    stubFetch({ reports: [REPORT] });
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    expect(
      await screen.findByText("c.martin@acme.example"),
    ).toBeInTheDocument();
    expect(screen.getByText("7c1e9b04aa11…")).toBeInTheDocument();
  });

  it("never claims conformity, on the page or in the dialog", async () => {
    stubFetch({
      agents: [{ name: "credit_review_agent" }],
      classifications: { credit_review_agent: COMPLETE_ENTRY },
      reports: [REPORT],
    });
    const user = userEvent.setup();
    const { baseElement } = renderWithProviders(<AiActPage />, {
      initialRoute: "/ai-act",
    });
    // The dialog portals outside the render container and carries the most
    // legally-loaded copy on the tab, so scan with it open, off baseElement.
    await user.click(
      await screen.findByRole("button", { name: /edit entry/i }),
    );
    await screen.findByRole("dialog");

    const rendered = baseElement.textContent ?? "";
    // The page's one legitimate use of each policed word: the disclaimer
    // denying conformity, and the proper names of the external checkers.
    // Everything else is a claim, so strip these and assert the rest is clean
    // — new copy is then guarded by default.
    expect(rendered).toContain(DISCLAIMER);
    const text = rendered
      .replace(DISCLAIMER, "")
      .replace(/compliance checker/gi, "");
    expect(text).not.toMatch(/\bcompliant\b/i);
    expect(text).not.toMatch(/\bcompliance\b/i);
    expect(text).not.toMatch(/\bconformity\b/i);
  });

  it("shows a load failure instead of an empty report history", async () => {
    // `?? []` would render "No reports yet" for a 500 — the opposite of the
    // truth, on the one question this page exists to answer.
    stubFetch({ reportsStatus: 500 });
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    expect(
      await screen.findByText("Couldn't load the report history"),
    ).toBeInTheDocument();
    expect(screen.queryByText("No reports yet")).toBeNull();
  });

  it("marks an entry that failed to load unavailable, with no edit button", async () => {
    // Opening the dialog on an unloaded entry would seed it blank, and saving
    // that overwrites the operator's assertion and its provenance.
    stubFetch({
      agents: [{ name: "credit_review_agent" }, { name: "support_agent" }],
      classifications: { support_agent: COMPLETE_ENTRY },
      classificationStatus: { credit_review_agent: 500 },
    });
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    expect(await screen.findByText("Unavailable")).toBeInTheDocument();
    // The failed agent is counted as neither complete nor incomplete.
    expect(
      screen.getByText(
        /2 registered · 1 complete · 0 incomplete · 1 unavailable/,
      ),
    ).toBeInTheDocument();
    // One editable row (the one that loaded), not two.
    expect(screen.getAllByRole("button", { name: /entry$/i })).toHaveLength(1);
  });

  it("reads a 404 classification as no entry recorded, not as a failure", async () => {
    // PR 1 may answer a missing row with a 404 rather than a prefilled body;
    // either way the row is "incomplete", never "unavailable".
    stubFetch({
      agents: [{ name: "devops_agent" }],
      classificationStatus: { devops_agent: 404 },
    });
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    expect(await screen.findByText("Incomplete")).toBeInTheDocument();
    expect(
      screen.getByText("Missing: Classification entry"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Unavailable")).toBeNull();
  });

  it("renders the report period in UTC, matching the picker and the annex", async () => {
    // A period ending 2026-09-11T23:59:59.999Z is 12 Sep locally in Paris;
    // the history row must show the day the operator picked.
    stubFetch({
      reports: [{ ...REPORT, period_end: "2026-09-11T23:59:59.999Z" }],
    });
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    const expected = `${new Date("2026-03-15T00:00:00Z").toLocaleDateString(
      undefined,
      { timeZone: "UTC" },
    )} → ${new Date("2026-09-11T23:59:59.999Z").toLocaleDateString(undefined, {
      timeZone: "UTC",
    })}`;
    expect(await screen.findByText(expected)).toBeInTheDocument();
  });

  it("falls back to the user id when the server resolves no email", async () => {
    // `generated_by_email` is a display convenience the spec's AiActReport
    // does not promise; the column must stay readable without it.
    const withoutEmail: AiActReport = { ...REPORT };
    delete withoutEmail.generated_by_email;
    stubFetch({ reports: [withoutEmail] });
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    expect(await screen.findByText("usr_1")).toBeInTheDocument();
  });
});
