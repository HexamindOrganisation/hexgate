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
 *   9. Generation in flight is visible, and a refusal is shown verbatim.
 *  10. No copy on the page — dialog included — claims conformity.
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
import { toast } from "sonner";

import type { AgentClassificationRead, AiActReport } from "@/lib/api";
import { useActive } from "@/lib/active";
import { AiActPage } from "@/routes/AiAct";
import { renderWithProviders } from "@/test/render";

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), success: vi.fn(), info: vi.fn() },
}));

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

/** What the GET serves for an agent with no entry: 200, `recorded: false`,
 * and the server's own missing-field list in its own field names. */
const EMPTY_ENTRY: AgentClassificationRead = {
  agent_name: "devops_agent",
  intended_purpose: null,
  operator_role: null,
  risk_tier: null,
  annex_iii_point: null,
  oversight_owner_name: null,
  oversight_owner_contact: null,
  checker_last_update_date: null,
  recorded: false,
  recorded_by_user_id: null,
  recorded_at: null,
  complete: false,
  missing_fields: [
    "intended_purpose",
    "operator_role",
    "risk_tier",
    "oversight_owner_name",
  ],
};

const COMPLETE_ENTRY: AgentClassificationRead = {
  agent_name: "credit_review_agent",
  intended_purpose: "Prepares creditworthiness assessments.",
  operator_role: "deployer",
  risk_tier: "high_risk",
  annex_iii_point: "5(b)",
  oversight_owner_name: "C. Martin",
  oversight_owner_contact: "c.martin@acme.example",
  checker_last_update_date: "2026-05-14",
  recorded: true,
  recorded_by_user_id: "usr_1",
  recorded_at: "2026-06-02T09:00:00Z",
  complete: true,
  missing_fields: [],
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
  annex_bytes: 20480,
  annex_filename: "rpt_1-annex.json",
  signing_kid: "hexgate-root-2026-01",
  signature_b64: "c2lnbmF0dXJl",
};

interface StubOptions {
  agents?: { name: string }[];
  classifications?: Record<string, AgentClassificationRead>;
  reports?: AiActReport[];
  /** Agent names whose classification GET fails with this status. */
  classificationStatus?: Record<string, number>;
  /** Agent names whose classification GET succeeds once, then fails — a
   * refetch blip on an entry already on screen. */
  classificationFailsAfterFirst?: string[];
  /** The agent list succeeds once, then fails — a refetch blip over a list
   * that is already cached and on screen. */
  agentsFailAfterFirst?: boolean;
  /** Status for GET .../ai-act/reports; 200 unless set. */
  reportsStatus?: number;
  /** Status for POST .../ai-act/report; 201 unless set. */
  generateStatus?: number;
  generateDetail?: string;
}

function stubFetch({
  agents = [],
  classifications = {},
  reports = [],
  classificationStatus = {},
  classificationFailsAfterFirst = [],
  agentsFailAfterFirst = false,
  reportsStatus = 200,
  generateStatus = 201,
  generateDetail = "nope",
}: StubOptions = {}): Call[] {
  const calls: Call[] = [];
  const classificationGets: Record<string, number> = {};
  let agentGets = 0;
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
            recorded: true,
            recorded_by_user_id: "usr_1",
            recorded_at: "2026-09-17T10:00:00Z",
            complete: false,
            missing_fields: ["operator_role", "risk_tier"],
          });
        case !!classification: {
          const name = (classification as RegExpMatchArray)[1];
          const status = classificationStatus[name];
          if (status) return json({ detail: "nope" }, status);
          if (classificationFailsAfterFirst.includes(name)) {
            const seen = (classificationGets[name] ?? 0) + 1;
            classificationGets[name] = seen;
            if (seen > 1) return json({ detail: "nope" }, 500);
          }
          return json(classifications[name] ?? EMPTY_ENTRY);
        }
        case url.pathname === `${base}/agents`: {
          agentGets += 1;
          if (agentsFailAfterFirst && agentGets > 1) {
            return json({ detail: "nope" }, 500);
          }
          return json(agents);
        }
        case url.pathname === `${base}/ai-act/report` && method === "POST":
          return generateStatus === 201
            ? // POST answers with the summary plus the parsed annex.
              json({ ...REPORT, annex: { report_id: REPORT.id } }, 201)
            : json({ detail: generateDetail }, generateStatus);
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
        "Missing: Intended purpose, Your role, Risk tier, Human-oversight owner",
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
    // The GET prefills intended_purpose from the manifest but reports the
    // entry unrecorded, with every required field still missing — a prefill is
    // Hexgate's suggestion, not the operator's assertion.
    stubFetch({
      agents: [{ name: "support_agent" }],
      classifications: {
        support_agent: {
          ...EMPTY_ENTRY,
          agent_name: "support_agent",
          intended_purpose: "Answers questions about existing accounts.",
        },
      },
    });
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    expect(await screen.findByText("Incomplete")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Record entry" }),
    ).toBeInTheDocument();
  });

  it("names the missing fields the server named, in the server's order", async () => {
    // Completeness is the endpoint's verdict, not a rule re-derived here, so
    // the report and this tab can never disagree about it. A high-risk entry
    // missing its Annex III point is the case where the two lists differ.
    stubFetch({
      agents: [{ name: "credit_review_agent" }],
      classifications: {
        credit_review_agent: {
          ...COMPLETE_ENTRY,
          annex_iii_point: null,
          complete: false,
          missing_fields: ["annex_iii_point"],
        },
      },
    });
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    expect(
      await screen.findByText("Missing: Annex III point"),
    ).toBeInTheDocument();
    expect(screen.getByText("Incomplete")).toBeInTheDocument();
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

  it("when the pdf route is not deployed then it says so instead of failing", async () => {
    // The stub 404s every unrouted path, which is what the un-deployed PDF
    // route (#237) actually does. The operator can do nothing about it, so
    // this must not read as a broken download.
    stubFetch({ reports: [REPORT] });
    const user = userEvent.setup();
    // `restoreAllMocks` does not clear a module mock's call history, so the
    // "no error toast" assertion below would otherwise pass or fail on this
    // test's position in the file rather than on the behaviour.
    vi.mocked(toast.error).mockClear();
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    await user.click(await screen.findByRole("button", { name: /pdf/i }));

    await waitFor(() =>
      expect(toast.info).toHaveBeenCalledWith(
        "PDF rendering is not available yet — download the annex.",
      ),
    );
    expect(toast.error).not.toHaveBeenCalled();
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

  it("shows the generation in flight, then the new report", async () => {
    let release: (() => void) | undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const calls = stubFetch({ reports: [] });
    const inner = vi.mocked(window.fetch).getMockImplementation();
    // Hold the POST open so the in-flight state is observable; every other
    // call passes straight through.
    vi.spyOn(window, "fetch").mockImplementation(async (input, init) => {
      if ((init?.method ?? "GET") === "POST") await gate;
      return inner!(input, init);
    });
    const user = userEvent.setup();
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    await user.click(
      await screen.findByRole("button", { name: /generate report/i }),
    );
    expect(
      await screen.findByText(/Assembling and signing the report/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /generating/i })).toBeDisabled();

    release!();
    await waitFor(() =>
      expect(screen.queryByText(/Assembling and signing/)).toBeNull(),
    );
    expect(calls.some((c) => c.method === "POST")).toBe(true);
  });

  it("shows the endpoint's own refusal of a period verbatim", async () => {
    // A 400 says why — "entirely older than the 180-day retention window" —
    // and that is more than this page could infer, so it is not swallowed.
    const detail =
      "the requested period is entirely older than the 180-day retention " +
      "window, so no records survive in it to report on";
    stubFetch({ generateStatus: 400, generateDetail: detail });
    const user = userEvent.setup();
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    await user.click(
      await screen.findByRole("button", { name: /generate report/i }),
    );
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(detail));
    // The in-flight banner clears on a refusal too — tying it to success only
    // would leave it claiming a signing that already failed.
    expect(screen.queryByText(/Assembling and signing/)).toBeNull();
    expect(
      screen.getByRole("button", { name: /generate report/i }),
    ).toBeEnabled();
  });

  it("refuses to save a classification that asserts nothing", async () => {
    // The PUT replaces the entry, so an empty body would wipe a recorded one
    // and stamp a recorder against no assertions; the endpoint 422s it.
    const calls = stubFetch({ agents: [{ name: "devops_agent" }] });
    const user = userEvent.setup();
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    await user.click(
      await screen.findByRole("button", { name: /record entry/i }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByRole("button", { name: /save entry/i }),
    ).toBeDisabled();

    // Whitespace is not an assertion: the endpoint normalises blanks to null
    // and then rejects the all-null body, so Save must stay disabled.
    await user.type(
      within(dialog).getByLabelText("Human-oversight owner"),
      "   ",
    );
    expect(
      within(dialog).getByRole("button", { name: /save entry/i }),
    ).toBeDisabled();

    await user.type(
      within(dialog).getByLabelText("Human-oversight owner"),
      "L. Okafor",
    );
    expect(
      within(dialog).getByRole("button", { name: /save entry/i }),
    ).toBeEnabled();
    expect(calls.some((c) => c.method === "PUT")).toBe(false);
  });

  it("saves the annex under the filename the server chose", async () => {
    stubFetch({
      reports: [{ ...REPORT, annex_filename: "rpt_1-annex.json" }],
    });
    const anchors: HTMLAnchorElement[] = [];
    const create = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation((tag: string) => {
      const el = create(tag);
      if (tag === "a") anchors.push(el as HTMLAnchorElement);
      return el;
    });
    const user = userEvent.setup();
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    await user.click(await screen.findByRole("button", { name: /annex/i }));

    await waitFor(() =>
      expect(anchors.some((a) => a.download === "rpt_1-annex.json")).toBe(true),
    );
  });

  it("shows the saved entry even when the refetch after it fails", async () => {
    // The PUT returns the stored entry, so the row is updated from the
    // response. Depending on the invalidation refetch instead would leave the
    // pre-save values on screen under a "recorded" toast, with nothing
    // scheduled to correct them (refetchOnWindowFocus is off).
    stubFetch({
      agents: [{ name: "credit_review_agent" }],
      classifications: { credit_review_agent: COMPLETE_ENTRY },
      classificationFailsAfterFirst: ["credit_review_agent"],
    });
    const user = userEvent.setup();
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    expect(await screen.findByText("Complete")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /edit entry/i }));
    const dialog = await screen.findByRole("dialog");
    await user.click(
      within(dialog).getByRole("button", { name: /save entry/i }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    // The stub's PUT answers with an entry the server counts as incomplete;
    // that is what the row must now show — not the pre-save "Complete", and
    // not "Unavailable" because the refetch behind it 500'd.
    expect(
      await screen.findByText("Missing: Your role, Risk tier"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Unavailable")).toBeNull();
    expect(
      screen.getByRole("button", { name: /edit entry/i }),
    ).toBeInTheDocument();
  });

  it("renders a risk tier it does not recognise rather than a dash", async () => {
    // `risk_tier` is a plain string on the read schema. A tier this build has
    // no label for is still a recorded assertion — showing the "not asserted"
    // dash beside a Complete badge would read as a gap that isn't there.
    stubFetch({
      agents: [{ name: "credit_review_agent" }],
      classifications: {
        credit_review_agent: {
          ...COMPLETE_ENTRY,
          risk_tier: "limited_risk" as never,
        },
      },
    });
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    expect(await screen.findByText("limited_risk")).toBeInTheDocument();
  });

  it("keeps the chosen period across a project switch", async () => {
    // The panel must not remount on a project change: re-running the period
    // defaults would silently widen a narrowed window back to 180 days, and
    // the next Generate would sign a report over the wrong one.
    stubFetch();
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    const from = await screen.findByLabelText("From (UTC)");
    fireEvent.change(from, { target: { value: "2026-08-01" } });

    act(() => {
      useActive.setState({ activeProjectId: "p2" });
    });
    act(() => {
      useActive.setState({ activeProjectId: PROJECT });
    });

    expect(screen.getByLabelText("From (UTC)")).toHaveValue("2026-08-01");
  });

  it("keeps the inventory counts when an agent-list refetch blips", async () => {
    // The header and the table have to state what they know on one rule: a
    // populated table under a headline with no numbers is the panel
    // contradicting itself about whether it knows the inventory.
    stubFetch({
      agents: [{ name: "credit_review_agent" }],
      classifications: { credit_review_agent: COMPLETE_ENTRY },
      agentsFailAfterFirst: true,
    });
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    expect(
      await screen.findByText(/1 registered · 1 complete · 0 incomplete/),
    ).toBeInTheDocument();

    // Leave the project and come back: the return refetch 500s while the
    // cached list is still what the table is drawing.
    act(() => {
      useActive.setState({ activeProjectId: "p2" });
    });
    act(() => {
      useActive.setState({ activeProjectId: PROJECT });
    });

    expect(await screen.findByText("credit_review_agent")).toBeInTheDocument();
    expect(
      screen.getByText(/1 registered · 1 complete · 0 incomplete/),
    ).toBeInTheDocument();
  });

  it("does not drop the Annex III point when editing an entry whose tier it cannot render", async () => {
    // The PUT is a full replace. A tier this build has no option for is one
    // whose Annex III field the form never showed, so clearing it on the
    // operator's behalf would delete a recorded reference from the next
    // signed report. The tier itself must show as itself, not as a blank box.
    const calls = stubFetch({
      agents: [{ name: "credit_review_agent" }],
      classifications: {
        credit_review_agent: {
          ...COMPLETE_ENTRY,
          risk_tier: "limited_risk" as never,
        },
      },
    });
    const user = userEvent.setup();
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    await user.click(
      await screen.findByRole("button", { name: /edit entry/i }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText("Risk tier")).toHaveTextContent(
      "limited_risk",
    );

    await user.type(
      within(dialog).getByLabelText("Owner contact (optional)"),
      "x",
    );
    await user.click(
      within(dialog).getByRole("button", { name: /save entry/i }),
    );

    await waitFor(() => {
      const put = calls.find((c) => c.method === "PUT");
      expect(put?.body).toMatchObject({
        risk_tier: "limited_risk",
        annex_iii_point: "5(b)",
      });
    });
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
    // The endpoint resolves the email server-side and sends null when the
    // account row is gone; the column must stay readable in that case.
    stubFetch({ reports: [{ ...REPORT, generated_by_email: null }] });
    renderWithProviders(<AiActPage />, { initialRoute: "/ai-act" });

    expect(await screen.findByText("usr_1")).toBeInTheDocument();
  });
});
