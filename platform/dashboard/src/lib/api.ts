/** Routes that are reachable without a session cookie — never redirect
 * away from these on a 401, otherwise we'd bounce the sign-in form itself
 * back to /sign-in in a loop when the user has bad credentials. */
const PUBLIC_AUTH_PATHS = [
  "/sign-in",
  "/sign-up",
  "/forgot-password",
  "/reset-password",
  "/verify-email",
];

/** Thrown by ``request`` when the backend returns 401, after the global
 * "redirect to /sign-in" side-effect has fired. Tests assert against the
 * error type; production code rarely catches it (the redirect already
 * happened). */
export class UnauthenticatedError extends Error {
  constructor(message = "not authenticated") {
    super(message);
    this.name = "UnauthenticatedError";
  }
}

/** Thrown for any non-2xx response other than 401 — carries the parsed
 * detail when the backend returns one (FastAPI Users speaks JSON with
 * a ``detail`` field). Fields are declared explicitly (not via TS
 * constructor parameter properties) because the project's tsconfig
 * enables ``erasableSyntaxOnly``. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/** Human-readable message from a FastAPI error body. Handles the two shapes:
 * a string `detail` (HTTPException) and the validation-error array
 * `detail: [{loc, msg, type}, ...]` (422) — the latter would otherwise
 * `String()` to "[object Object]". Returns null when neither applies. */
function messageFromDetail(detail: unknown): string | null {
  if (typeof detail !== "object" || detail === null || !("detail" in detail)) {
    return null;
  }
  const d = (detail as { detail: unknown }).detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    const msgs = d
      .map((e) =>
        e && typeof e === "object" && "msg" in e
          ? String((e as { msg: unknown }).msg)
          : null,
      )
      .filter((m): m is string => !!m);
    if (msgs.length) return msgs.join("; ");
  }
  return null;
}

/** Shared transport: fires the request, applies the global 401 redirect and
 * turns every other non-2xx into an ``ApiError``. Returns the raw Response so
 * the JSON callers and the binary ones (the AI Act annex / PDF downloads)
 * go through one auth and error path. */
async function send(path: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(path, {
    ...init,
    // ``include`` so the hexgate_session cookie rides on cross-origin
    // dev (vite on 5173 → api on 8000) and on prod where the dashboard
    // and API may live on different subdomains.
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (res.status === 401) {
    // Single global redirect — every component that does a request gets
    // sent to sign-in if their session expired. Public auth routes are
    // exempt: a 401 there is just "wrong password", surface it normally.
    const onAuthPage = PUBLIC_AUTH_PATHS.some((p) =>
      window.location.pathname.startsWith(p),
    );
    if (!onAuthPage) {
      window.location.href = "/sign-in";
    }
    throw new UnauthenticatedError();
  }
  if (!res.ok) {
    let detail: unknown;
    let bodyText = "";
    try {
      bodyText = await res.text();
      detail = bodyText ? JSON.parse(bodyText) : null;
    } catch {
      detail = bodyText;
    }
    const message =
      messageFromDetail(detail) ?? `${res.status} ${res.statusText}`;
    throw new ApiError(res.status, detail, message);
  }
  return res;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await send(path, init);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** Binary GET. Same auth and error handling as ``request``; hands the body
 * back as a Blob for the caller to save to disk. */
async function requestBlob(path: string): Promise<Blob> {
  const res = await send(path);
  return await res.blob();
}

/** Mirror of platform/api/schemas.py:TokenListItem. Actor emails are resolved
 * server-side, so this never looks users up. ``created_by_*`` minted the key,
 * ``owner_*`` owns it; all four are null for a pre-actor-columns key. */
export interface TokenListItem {
  id: string;
  name: string;
  masked: string;
  scopes: string[];
  created_at: string;
  last_used_at: string | null;
  created_by_user_id: string | null;
  created_by_email: string | null;
  owner_user_id: string | null;
  owner_email: string | null;
}

export interface TokenMintResponse extends TokenListItem {
  full: string;
}

export interface TokenMintRequest {
  name: string;
  scopes?: string[];
  env?: "test" | "live";
  /** Whose key this is; omit for your own. Admins/owners only, and the target
   * must be in the project's org — 403 otherwise. */
  owner_user_id?: string;
}

export interface AgentRead {
  id: string;
  name: string;
  agent_yaml: string;
  /**
   * Canonical policy document. Flat single-policy YAML or — when the agent
   * declares per-role behaviour — an inline-roles YAML with a top-level
   * ``roles:`` map. See ``parseRolesFromPolicy`` in lib/policy.ts for the
   * client-side helper that extracts the role list (for the Playground
   * picker, etc).
   */
  policy_yaml: string;
  system_md: string;
  updated_at: string;
}

export interface AgentUpdate {
  agent_yaml?: string;
  policy_yaml?: string;
  system_md?: string;
}

/**
 * Tool input parameter, mirroring InputProperty in platform/api/schemas.py.
 */
export interface InputProperty {
  title: string;
  type: string;
}

/**
 * Tool input schema, mirroring InputSchema in platform/api/schemas.py.
 */
export interface InputSchema {
  properties: Record<string, InputProperty>;
  required: string[];
}

/**
 * Tool definition as stored in the registered manifest. ``description`` is
 * nullable on read-back to match the platform-side schema.
 */
export interface ToolDefinition {
  name: string;
  description: string | null;
  input_schema: InputSchema;
}

/** L3 contents of a skill, by name. */
export interface SkillResources {
  references: string[];
  assets: string[];
  scripts: string[];
}

/**
 * One skill on a registered manifest, mirroring SkillDefinition in
 * platform/api/schemas.py.
 *
 * ``resources`` is null when the agent's framework does not enumerate them
 * (deepagents discovers frontmatter only) — distinct from an object with empty
 * arrays, which means it does enumerate and the skill ships none.
 */
export interface SkillDefinition {
  name: string;
  description: string;
  source: string | null;
  resources: SkillResources | null;
  allowed_tools: string[];
  additional_tools: string[];
  content_hash: string | null;
}

/**
 * Registered manifest body, mirroring AgentManifest in platform/api/schemas.py.
 */
export interface AgentManifest {
  name: string;
  description: string | null;
  framework: string;
  model: string | null;
  system_prompt: string | null;
  tools: ToolDefinition[];
  /** Null for frameworks with no skill concept, and for pre-skills manifests. */
  skills: SkillDefinition[] | null;
}

/**
 * Dashboard-facing envelope for the latest registered manifest of an agent.
 *
 * ``manifest`` / ``version`` / ``content_hash`` are null when the agent
 * exists but has never been registered via ``POST /v1/agents``. ``name``
 * always reflects the Agent row's name (the picker uses it directly).
 */
export interface AgentManifestView {
  name: string;
  manifest: AgentManifest | null;
  version: number | null;
  content_hash: string | null;
  updated_at: string;
}

export interface PolicyValidationError {
  /** Role name when the failure was inside an inline-roles entry; null otherwise. */
  role: string | null;
  /** Tool the lint is about (`permissive-default`); null otherwise. Separate
   * from `role` because both render as a bare name in the same slot. */
  tool: string | null;
  line: number | null;
  message: string;
}

export interface ValidatePolicyResponse {
  ok: boolean;
  errors: PolicyValidationError[];
  /** Authoring lints. Never affect `ok`, so they can't block a save. */
  warnings: PolicyValidationError[];
}

// --- Compose policy files (multi-file editor; mirrors platform/api/schemas.py)
//
// A project's compose policy is a set of named files (a small in-DB
// filesystem). The entry file is `policy.yaml`; others are pulled in via
// `import:`. Files carry no tier — roles/imports live inside the content.

/** One stored compose file. `name` may contain slashes (e.g. `caps/refunds.yaml`). */
export interface PolicyFileRead {
  name: string;
  content: string;
  content_hash: string;
  updated_at: string;
}

/** One analyzer lint over the composed project. `source`/`tier`/`tool`/`role`
 * locate it; a null `role` is a project-wide concern. */
export interface PolicyLint {
  code: string;
  severity: "error" | "warning" | "info";
  message: string;
  source: string | null;
  tier: string | null;
  tool: string | null;
  role: string | null;
}

/** The effective policy for one role: a tool map plus the catch-all default.
 * Loosely typed — it's an AgentPolicy dump the editor only renders. */
export interface ResolvedRolePolicy {
  default_policy?: { mode?: string } | null;
  tools?: Record<
    string,
    { mode?: string; constraints?: string[] | null } | null
  >;
  [key: string]: unknown;
}

export type ResolvedPolicy = Record<string, ResolvedRolePolicy>;

/** A node in the resolved-policy graph: an agent, a tool, an MCP tool, or a
 * role (admission source). */
export interface PolicyGraphNode {
  id: string;
  kind: "agent" | "tool" | "mcp" | "role";
  label: string;
}

/** A directed edge: a tool `call`, an agent→agent `reach` (`via` tool/handoff),
 * or a role→agent `admission`. `verdict` is the composed mode; `constraints`
 * and `roles` feed the per-edge card. */
export interface PolicyGraphEdge {
  source: string;
  target: string;
  kind: "call" | "reach" | "admission";
  via?: "tool" | "handoff" | null;
  verdict: PolicyTestOutcome;
  constraints: string[];
  roles: string[];
}

export interface PolicyGraph {
  nodes: PolicyGraphNode[];
  edges: PolicyGraphEdge[];
  /** Every role across all agents (the role picker's options) — independent of
   * any role filter applied to the edges. */
  roles: string[];
}

/** The editor's unsaved edit of one file, overlaid before preview/test. */
export interface PolicyFileDraft {
  name: string;
  content: string;
}

export interface PolicyPreviewResponse {
  /** The effective policy per role, or null when the draft doesn't compose. */
  resolved: ResolvedPolicy | null;
  lints: PolicyLint[];
}

export type PolicyTestOutcome = "allow" | "deny" | "approval_required";

export interface PolicyTestRequest {
  role: string;
  /** The executing agent whose column to test against; defaults to "*". */
  agent?: string;
  tool: string;
  args?: Record<string, unknown>;
  attributes?: Record<string, unknown> | null;
  draft?: PolicyFileDraft | null;
}

export interface PolicyTestResponse {
  outcome: PolicyTestOutcome;
  reason: string | null;
  violations: string[];
  hint: string | null;
}

// --- Audit dashboard (mirrors platform/api/schemas.py) ----------------------

export type AuditWindow = "24h" | "7d" | "30d" | "90d";
export type AuditOutcome = "allow" | "deny" | "needs_approval";

export interface OutcomeCounts {
  all: number;
  allow: number;
  deny: number;
  needs_approval: number;
}

/** One agent/role/tool bucket; an empty role keeps its raw `""` key —
 * the dashboard maps it to the "(none)" display label locally.
 *
 * `by_role` counts membership — a caller carrying `["billing", "support"]`
 * lands in both — so it can sum to more than `totals`. Every other breakdown
 * stays one row per decision. */
export interface AuditBreakdownRow extends OutcomeCounts {
  key: string;
}

export interface AuditSummary {
  totals: OutcomeCounts;
  by_agent: AuditBreakdownRow[];
  by_role: AuditBreakdownRow[];
  by_tool: AuditBreakdownRow[];
  by_user: AuditBreakdownRow[];
}

/** One time bucket; `bucket` is an ISO string. */
export interface AuditTimeseriesPoint {
  bucket: string;
  allow: number;
  deny: number;
  needs_approval: number;
}

/** One events-table row; `hint`/`arguments`/`attributes` are decoded JSON. */
export interface AuditDecisionRow {
  event_id: string;
  occurred_at: string;
  received_at: string;
  agent_name: string;
  agent_version_id: string;
  session_id: string;
  user_id: string;
  tool_name: string;
  /** Distinct roles the caller carried, in order. Always the full set: an
   * SDK that sends the legacy scalar has it folded in at ingest. */
  user_roles: string[];
  /** Role whose policy granted or gated the call; `""` on a deny. */
  deciding_role: string;
  outcome: AuditOutcome;
  error_type: string;
  reason: string;
  violations: string[];
  hint: unknown;
  arguments: unknown;
  attributes: unknown;
  /** Run this decision belongs to; `null` outside any run scope. Pass it, and
   * `session_id`, to `GET /projects/{id}/audit/llm-messages` to scope this
   * decision's transcript — forward both verbatim, blanks included. See
   * `listLlmMessages`. */
  run_id: string | null;
}

export interface AuditDecisionPage {
  rows: AuditDecisionRow[];
  total: number;
  limit: number;
  offset: number;
}

/** Scope filters shared by summary/timeseries/list. `undefined` = no
 * filter; `role: ''` = the no-role bucket (sent as `role=`). A non-empty
 * `role` matches membership, so a multi-role call is returned under each. */
export interface AuditScope {
  window?: AuditWindow;
  agent?: string;
  role?: string;
  tool?: string;
  start_date?: string;
  end_date?: string;
  user?: string;
}

/** Narrow scope accepted by the anomalies endpoint (cross-user by design). */
export type AnomalyScope = Pick<
  AuditScope,
  "window" | "start_date" | "end_date"
>;

/** List filters: scope + table-only outcome/session_id + paging. */
export interface AuditDecisionFilters extends AuditScope {
  outcome?: AuditOutcome;
  session_id?: string;
  limit?: number;
  offset?: number;
}

export type BanType = "agent" | "user";

/** Mirror of platform/api/schemas.py:BanRead. ``active`` is the
 * server-computed ``revoked_at is None``; ``created_by_email`` is resolved
 * server-side for display (null when the account no longer exists). */
export interface BanRead {
  id: string;
  project_id: string;
  ban_type: BanType;
  target_agent_name: string | null;
  target_user_id: string | null;
  reason: string | null;
  created_by_user_id: string;
  created_by_email: string | null;
  created_at: string;
  revoked_at: string | null;
  active: boolean;
}

/** POST body for a ban. Only the target field matching ``ban_type`` is set —
 * the server's cross-validator rejects a body that sets the other target. */
export interface BanCreateBody {
  ban_type: BanType;
  target_agent_name?: string;
  target_user_id?: string;
  reason?: string;
}

/** One blocked-attempt row from GET …/audit/ban-enforcements. A ban is
 * refused before any tool call, so there's no tool/role/outcome —
 * mirrors platform/api/schemas.py:BanEnforcementRow. */
export interface BanEnforcementRow {
  event_id: string;
  occurred_at: string;
  received_at: string;
  agent_name: string;
  session_id: string;
  user_id: string;
  ban_type: "agent" | "user";
  ban_id: string;
  reason: string;
}

export interface BanEnforcementPage {
  rows: BanEnforcementRow[];
  total: number;
  limit: number;
  offset: number;
}

/** Filters for the blocked-attempts feed: window (or explicit range) +
 * paging. No agent/role/tool scope — the ban table carries none. */
export interface BanEnforcementFilters {
  window?: AuditWindow;
  start_date?: string;
  end_date?: string;
  limit?: number;
  offset?: number;
}

export type AnomalySeverity = "high" | "medium";

/** One per-user anomaly burst from GET /audit/anomalies. */
export interface AuditAnomaly {
  user_id: string;
  severity: AnomalySeverity;
  deny: number;
  all: number;
  deny_rate: number;
  first_seen: string;
  last_seen: string;
}

// --- Usage dashboard (mirrors platform/api/schemas.py LlmInvocation*) -------

/** Totals for a slice: call volume + token counts. */
export interface LlmInvocationTotals {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}

/** One model/agent/user bucket. */
export interface LlmInvocationBreakdownRow extends LlmInvocationTotals {
  key: string;
}

export interface LlmInvocationSummary {
  totals: LlmInvocationTotals;
  by_model: LlmInvocationBreakdownRow[];
  by_agent: LlmInvocationBreakdownRow[];
  by_user: LlmInvocationBreakdownRow[];
}

/** Scope filters for the usage summary. `undefined` = no filter. */
export interface LlmUsageScope {
  window?: AuditWindow;
  agent?: string;
  model?: string;
  user?: string;
  start_date?: string;
  end_date?: string;
}

// --- LLM message transcripts (mirrors schemas.py LlmMessageRow/Page) -------

/** One GenAI content part. `type` names it; the rest is shape-specific, so
 * the renderer reads what it knows and falls back to the raw object.
 *
 * Reasoning is the part to expect nothing about: the OpenAI Agents adapter
 * drops it from `output_messages` and keeps it on the input side as a whole
 * carried-through item, and whether the other adapters do the same is still
 * open (issue #221). The renderer prints any part it does not recognise,
 * which is what makes either answer safe. */
export interface LlmMessagePart {
  type?: string;
  [key: string]: unknown;
}

/** One GenAI message: a role plus its parts. */
export interface LlmMessage {
  role?: string;
  parts?: LlmMessagePart[];
  [key: string]: unknown;
}

/** One row of a session transcript. The three content fields are decoded
 * server-side from their stored JSON, so they arrive as the `gen_ai.*`
 * structures — except when the stored text no longer parses, in which case
 * the raw string comes back instead. Hence `unknown`. */
export interface LlmMessageRow {
  event_id: string;
  occurred_at: string;
  received_at: string;
  agent_name: string;
  agent_version_id: string;
  session_id: string;
  user_id: string;
  model: string;
  /** Which message list this event extends. One session holds several — the
   * main run, each sub-agent, each handoff — and `message_seq` restarts at 0
   * in each, so a gap is only a gap within one `turn_key`. */
  turn_key: string;
  message_seq: number;
  /** The event restated the whole list instead of extending it. */
  resynced: boolean;
  /** A content field was cut to its byte cap, by the SDK or the enricher. */
  truncated: boolean;
  input_messages: unknown;
  output_messages: unknown;
  system_instructions: unknown;
  run_id: string | null;
}

export interface LlmMessagePage {
  rows: LlmMessageRow[];
  total: number;
  limit: number;
  offset: number;
}

/** Rows per transcript window. See `listLlmMessages` for why it is not the
 * server's `MAX_PAGE_SIZE` of 100. */
export const LLM_MESSAGE_PAGE = 50;

// --- AI Act evidence report -------------------------------------------------
//
// Wire shapes for the two slices the tab talks to. Neither endpoint is merged
// yet, so these are pinned field-for-field to what the open branches serve —
// not to the spec's prose, which is looser than both:
//
//   classification  #235 vl/feat/agent_ai_act_classification @ 5d110f5d
//                   (hexgate_api/schemas.py: AgentClassificationRead/Write)
//   report          #239 vl/feat/ai_act_report @ 89a040d0
//                   (hexgate_api/schemas.py: AiActReportCreate/Summary/Read)

export type OperatorRole = "provider" | "deployer";

export type RiskTier = "high_risk" | "not_high_risk" | "prohibited" | "minimal";

/**
 * The operator's own Article 6 / Annex III assertion about one agent, as
 * `GET …/agents/{name}/classification` returns it.
 *
 * An agent with no entry gets 200 rather than 404: the endpoint serves an
 * empty entry with `recorded: false` and `intended_purpose` prefilled from
 * the registered manifest description, so the form has something to open on.
 * Nothing counts as recorded until the operator PUTs it. (An agent the
 * project does not have is still a 404.)
 *
 * `complete` and `missing_fields` are the server's, not re-derived here — the
 * endpoint computes them so the report and this tab can never disagree about
 * whether an entry counts. `missing_fields` carries wire field names in a
 * fixed order (`intended_purpose`, `operator_role`, `risk_tier`,
 * `annex_iii_point` when the tier is high-risk, `oversight_owner_name`);
 * `CLASSIFICATION_FIELD_LABELS` in lib/ai-act.ts turns them into display copy.
 */
export interface AgentClassificationRead {
  agent_name: string;
  intended_purpose: string | null;
  operator_role: OperatorRole | null;
  risk_tier: RiskTier | null;
  /** Annex III reference, e.g. `"5(b)"`. Required only for a high-risk tier. */
  annex_iii_point: string | null;
  oversight_owner_name: string | null;
  oversight_owner_contact: string | null;
  /** `last_update_date` (ISO `YYYY-MM-DD`) of the compliance checker the
   * operator relied on when recording the entry. */
  checker_last_update_date: string | null;
  /** False until the operator has saved an entry. */
  recorded: boolean;
  recorded_by_user_id: string | null;
  recorded_at: string | null;
  complete: boolean;
  missing_fields: string[];
}

/**
 * PUT body — the assertion fields only. Provenance (`recorded_by_user_id`,
 * `recorded_at`) is stamped server-side from the session.
 *
 * A full replace, not a patch: an omitted field means the operator cleared
 * it. The endpoint sets `extra="forbid"`, so an unknown key is a 422 rather
 * than a silently ignored one, and rejects a body that asserts nothing at all
 * — `assertsSomething` in lib/ai-act.ts is the client-side half of that rule.
 */
export interface AgentClassificationUpdate {
  intended_purpose: string | null;
  operator_role: OperatorRole | null;
  risk_tier: RiskTier | null;
  annex_iii_point: string | null;
  oversight_owner_name: string | null;
  oversight_owner_contact: string | null;
  checker_last_update_date: string | null;
}

/**
 * One generated evidence report, as `GET …/ai-act/reports` lists it (newest
 * first).
 *
 * `annex_json` — the signed bytes — is not carried here: the annex is
 * downloaded on demand so the history list stays small. What it does carry is
 * everything a verifier needs to check those bytes once downloaded: the
 * digest, the byte length they should have, the signing key id and the
 * base64 signature.
 */
export interface AiActReport {
  id: string;
  project_id: string;
  period_start: string;
  period_end: string;
  generated_at: string;
  generated_by_user_id: string;
  /** Resolved server-side for display; null when the account is gone. */
  generated_by_email: string | null;
  annex_sha256: string;
  /** Length of the signed annex in bytes. */
  annex_bytes: number;
  /** Download name the annex endpoint also sets in Content-Disposition. */
  annex_filename: string;
  signing_kid: string;
  /** Ed25519 signature over the digest, base64. */
  signature_b64: string;
}

/** What `POST …/ai-act/report` returns (201): the history row plus the parsed
 * annex. The parsed object is a convenience for a renderer — the signature
 * covers the bytes the annex download serves, not a re-serialization of it. */
export interface AiActReportRead extends AiActReport {
  annex: unknown;
}

/** POST body. Both bounds are optional; omitting them asks the server for
 * the full retention window ending now. */
export interface AiActReportCreateBody {
  from?: string;
  to?: string;
}

/** Percent-encode each path segment but keep the slashes, so a name like
 * `caps/refunds.yaml` reaches the server's `{name:path}` converter intact. */
function encodePath(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}

function qs(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    // undefined = omit. '' is kept: `role=` (empty value) is meaningful —
    // it selects the no-role bucket server-side.
    if (value !== undefined) search.set(key, String(value));
  }
  const str = search.toString();
  return str ? `?${str}` : "";
}

/**
 * Project-scoped API surface. ``projectId`` is required on every method
 * — there's no fallback constant. Callers read it from
 * :func:`useProjectScoped` and the page only mounts these calls once
 * the scope resolves to ``ready``.
 */
export const api = {
  listTokens: (projectId: string) =>
    request<TokenListItem[]>(`/v1/projects/${projectId}/tokens`),

  mintToken: (body: TokenMintRequest, projectId: string) =>
    request<TokenMintResponse>(`/v1/projects/${projectId}/tokens`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  revokeToken: (tokenId: string, projectId: string) =>
    request<void>(`/v1/projects/${projectId}/tokens/${tokenId}`, {
      method: "DELETE",
    }),

  listAgents: (projectId: string) =>
    request<AgentRead[]>(`/v1/projects/${projectId}/agents`),

  listAgentManifests: (projectId: string) =>
    request<AgentManifestView[]>(`/v1/projects/${projectId}/agents/manifest`),

  getAgent: (name: string, projectId: string) =>
    request<AgentRead>(`/v1/projects/${projectId}/agents/${name}`),

  updateAgent: (name: string, body: AgentUpdate, projectId: string) =>
    request<AgentRead>(`/v1/projects/${projectId}/agents/${name}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  validatePolicy: (name: string, policy_yaml: string, projectId: string) =>
    request<ValidatePolicyResponse>(
      `/v1/projects/${projectId}/agents/${name}/validate`,
      { method: "POST", body: JSON.stringify({ policy_yaml }) },
    ),

  getAuditSummary: (scope: AuditScope, projectId: string) =>
    request<AuditSummary>(
      `/v1/projects/${projectId}/audit/summary${qs({ ...scope })}`,
    ),

  getAuditTimeseries: (scope: AuditScope, projectId: string) =>
    request<AuditTimeseriesPoint[]>(
      `/v1/projects/${projectId}/audit/timeseries${qs({ ...scope })}`,
    ),

  listAuditDecisions: (filters: AuditDecisionFilters, projectId: string) =>
    request<AuditDecisionPage>(
      `/v1/projects/${projectId}/audit/decisions${qs({ ...filters })}`,
    ),

  getAuditAnomalies: (scope: AnomalyScope, projectId: string) =>
    request<AuditAnomaly[]>(
      `/v1/projects/${projectId}/audit/anomalies${qs({ ...scope })}`,
    ),

  listBanEnforcements: (filters: BanEnforcementFilters, projectId: string) =>
    request<BanEnforcementPage>(
      `/v1/projects/${projectId}/audit/ban-enforcements${qs({ ...filters })}`,
    ),

  listBans: (projectId: string, includeRevoked = false) =>
    request<BanRead[]>(
      `/v1/projects/${projectId}/bans${
        includeRevoked ? "?include_revoked=true" : ""
      }`,
    ),

  createBan: (body: BanCreateBody, projectId: string) =>
    request<BanRead>(`/v1/projects/${projectId}/bans`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  revokeBan: (banId: string, projectId: string) =>
    request<void>(`/v1/projects/${projectId}/bans/${banId}`, {
      method: "DELETE",
    }),

  getLlmUsageSummary: (scope: LlmUsageScope, projectId: string) =>
    request<LlmInvocationSummary>(
      `/v1/projects/${projectId}/llm/summary${qs({ ...scope })}`,
    ),

  /**
   * One decision's transcript, scoped by session and/or run.
   *
   * Both scopes go out exactly as the decision row held them, blanks
   * included: `?session_id=&run_id=…` is the intended request, not a
   * malformed one. `session_id` is the second column of the storage sort
   * key, so pinning it — even to `""` — keeps the scan inside one
   * contiguous block; omitting it means "I do not know the session" and
   * makes a run-scoped read scan every session in the project. A null
   * `run_id` goes out blank, which the server reads as absent.
   *
   * The caller must not send two blanks: that names no transcript and is a
   * 422 by design.
   *
   * Rows come back oldest-first, so a page is a *window* on the transcript
   * and `offset` chooses which. The default is the server's own default,
   * half its `MAX_PAGE_SIZE` ceiling: one row carries up to ~272 KiB of
   * capped content, so asking for the ceiling would put ~27 MiB on the wire
   * (uncompressed — the API installs no gzip) to render the few turns the
   * drawer expands.
   */
  listLlmMessages: (
    projectId: string,
    sessionId: string,
    runId: string | null,
    limit = LLM_MESSAGE_PAGE,
    offset = 0,
  ) =>
    request<LlmMessagePage>(
      `/v1/projects/${projectId}/audit/llm-messages${qs({
        session_id: sessionId,
        run_id: runId ?? "",
        limit,
        offset,
      })}`,
    ),

  // --- Compose policy files ---

  listPolicyFiles: (projectId: string) =>
    request<PolicyFileRead[]>(`/v1/projects/${projectId}/policy-files`),

  upsertPolicyFile: (projectId: string, name: string, content: string) =>
    request<PolicyFileRead>(
      `/v1/projects/${projectId}/policy-files/${encodePath(name)}`,
      { method: "PUT", body: JSON.stringify({ content }) },
    ),

  deletePolicyFile: (projectId: string, name: string) =>
    request<void>(
      `/v1/projects/${projectId}/policy-files/${encodePath(name)}`,
      { method: "DELETE" },
    ),

  resolvePolicy: (projectId: string, role?: string, agent?: string) =>
    request<{ roles: ResolvedPolicy }>(
      `/v1/projects/${projectId}/policy/resolve${qs({ role, agent })}`,
    ).then((r) => r.roles),

  checkPolicy: (projectId: string) =>
    request<{ ok: boolean; lints: PolicyLint[] }>(
      `/v1/projects/${projectId}/policy/check`,
    ),

  policyGraph: (projectId: string, role?: string) =>
    request<PolicyGraph>(
      `/v1/projects/${projectId}/policy/graph${qs({ role })}`,
    ),

  previewPolicy: (projectId: string, draft: PolicyFileDraft, agent?: string) =>
    request<PolicyPreviewResponse>(`/v1/projects/${projectId}/policy/preview`, {
      method: "POST",
      body: JSON.stringify({ ...draft, agent: agent ?? "*" }),
    }),

  testPolicy: (projectId: string, body: PolicyTestRequest) =>
    request<PolicyTestResponse>(`/v1/projects/${projectId}/policy/test`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // --- AI Act evidence report ---

  getAgentClassification: (name: string, projectId: string) =>
    request<AgentClassificationRead>(
      `/v1/projects/${projectId}/agents/${name}/classification`,
    ),

  putAgentClassification: (
    name: string,
    body: AgentClassificationUpdate,
    projectId: string,
  ) =>
    request<AgentClassificationRead>(
      `/v1/projects/${projectId}/agents/${name}/classification`,
      { method: "PUT", body: JSON.stringify(body) },
    ),

  generateAiActReport: (body: AiActReportCreateBody, projectId: string) =>
    request<AiActReportRead>(`/v1/projects/${projectId}/ai-act/report`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  listAiActReports: (projectId: string) =>
    request<AiActReport[]>(`/v1/projects/${projectId}/ai-act/reports`),

  /** The signed annex — the canonical artifact the signature covers. */
  downloadAiActAnnex: (reportId: string, projectId: string) =>
    requestBlob(`/v1/projects/${projectId}/ai-act/reports/${reportId}/annex`),

  /** The PDF rendering of the same annex — PR 4 (#237), stacked on #239, so
   * this 404s until that lands. */
  downloadAiActReportPdf: (reportId: string, projectId: string) =>
    requestBlob(`/v1/projects/${projectId}/ai-act/reports/${reportId}.pdf`),
};
