export const DEFAULT_PROJECT_ID = 'support-bot'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  })
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`${res.status} ${res.statusText}: ${body}`)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export interface TokenListItem {
  id: string
  name: string
  masked: string
  scopes: string[]
  created_at: string
  last_used_at: string | null
}

export interface TokenMintResponse extends TokenListItem {
  full: string
}

export interface TokenMintRequest {
  name: string
  scopes?: string[]
  env?: 'test' | 'live'
}

export interface AgentRead {
  id: string
  name: string
  agent_yaml: string
  /**
   * Canonical policy document. Flat single-policy YAML or — when the agent
   * declares per-role behaviour — an inline-roles YAML with a top-level
   * ``roles:`` map. See ``parseRolesFromPolicy`` in lib/policy.ts for the
   * client-side helper that extracts the role list (for the Playground
   * picker, etc).
   */
  policy_yaml: string
  system_md: string
  updated_at: string
}

export interface AgentUpdate {
  agent_yaml?: string
  policy_yaml?: string
  system_md?: string
}

/**
 * Tool input parameter, mirroring InputProperty in platform/api/schemas.py.
 */
export interface InputProperty {
  title: string
  type: string
}

/**
 * Tool input schema, mirroring InputSchema in platform/api/schemas.py.
 */
export interface InputSchema {
  properties: Record<string, InputProperty>
  required: string[]
}

/**
 * Tool definition as stored in the registered manifest. ``description`` is
 * nullable on read-back to match the platform-side schema.
 */
export interface ToolDefinition {
  name: string
  description: string | null
  input_schema: InputSchema
}

/**
 * Registered manifest body, mirroring AgentManifest in platform/api/schemas.py.
 */
export interface AgentManifest {
  name: string
  description: string | null
  framework: string
  model: string | null
  system_prompt: string | null
  tools: ToolDefinition[]
}

/**
 * Dashboard-facing envelope for the latest registered manifest of an agent.
 *
 * ``manifest`` / ``version`` / ``content_hash`` are null when the agent
 * exists but has never been registered via ``POST /v1/agents``. ``name``
 * always reflects the Agent row's name (the picker uses it directly).
 */
export interface AgentManifestView {
  name: string
  manifest: AgentManifest | null
  version: number | null
  content_hash: string | null
  updated_at: string
}

export interface PolicyValidationError {
  /** Role name when the failure was inside an inline-roles entry; null otherwise. */
  role: string | null
  line: number | null
  message: string
}

export interface ValidatePolicyResponse {
  ok: boolean
  errors: PolicyValidationError[]
}

// --- Audit dashboard --------------------------------------------------------
// Mirror the read models in platform/api/schemas.py. Windows are bounded by the
// 90-day storage TTL; "(none)" is the breakdown label for an empty agent/role.

export type AuditWindow = '24h' | '7d' | '30d' | '90d'
export type AuditOutcome = 'allow' | 'deny' | 'needs_approval'

export interface OutcomeCounts {
  all: number
  allow: number
  deny: number
  needs_approval: number
}

/** One categorical bucket (agent / role / tool). `key` is "(none)" when empty. */
export interface AuditBreakdownRow extends OutcomeCounts {
  key: string
}

/** One top-denial-reason bucket: `key` is the reason text, `n` its count. */
export interface AuditReasonRow {
  key: string
  n: number
}

export interface AuditSummary {
  totals: OutcomeCounts
  by_agent: AuditBreakdownRow[]
  by_role: AuditBreakdownRow[]
  by_tool: AuditBreakdownRow[]
  by_reason: AuditReasonRow[]
}

/** One time bucket of the outcome-over-time chart. `bucket` is an ISO string. */
export interface AuditTimeseriesPoint {
  bucket: string
  allow: number
  deny: number
  needs_approval: number
}

/** One detail row. `hint`/`arguments` are decoded JSON (object, or raw string). */
export interface AuditDecisionRow {
  event_id: string
  occurred_at: string
  received_at: string
  agent_name: string
  agent_version_id: string
  session_id: string
  user_id: string
  tool_name: string
  role: string
  outcome: AuditOutcome
  error_type: string
  reason: string
  violations: string[]
  hint: unknown
  arguments: unknown
}

export interface AuditDecisionPage {
  rows: AuditDecisionRow[]
  total: number
  limit: number
  offset: number
}

/** Scope filters shared by summary/timeseries/list — they narrow the slice the
 * KPIs, charts and breakdowns all reflect. Pass `role: '(none)'` for no-role. */
export interface AuditScope {
  window?: AuditWindow
  agent?: string
  role?: string
  tool?: string
  q?: string
}

/** Decisions list filters: scope + table-only outcome/session_id + paging. */
export interface AuditDecisionFilters extends AuditScope {
  outcome?: AuditOutcome
  session_id?: string
  limit?: number
  offset?: number
}

function qs(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value))
  }
  const str = search.toString()
  return str ? `?${str}` : ''
}

export const api = {
  listTokens: (projectId = DEFAULT_PROJECT_ID) =>
    request<TokenListItem[]>(`/v1/projects/${projectId}/tokens`),

  mintToken: (body: TokenMintRequest, projectId = DEFAULT_PROJECT_ID) =>
    request<TokenMintResponse>(`/v1/projects/${projectId}/tokens`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  revokeToken: (tokenId: string, projectId = DEFAULT_PROJECT_ID) =>
    request<void>(`/v1/projects/${projectId}/tokens/${tokenId}`, {
      method: 'DELETE',
    }),

  listAgents: (projectId = DEFAULT_PROJECT_ID) =>
    request<AgentRead[]>(`/v1/projects/${projectId}/agents`),

  listAgentManifests: (projectId = DEFAULT_PROJECT_ID) =>
    request<AgentManifestView[]>(
      `/v1/projects/${projectId}/agents/manifest`,
    ),

  getAgent: (name: string, projectId = DEFAULT_PROJECT_ID) =>
    request<AgentRead>(`/v1/projects/${projectId}/agents/${name}`),

  updateAgent: (name: string, body: AgentUpdate, projectId = DEFAULT_PROJECT_ID) =>
    request<AgentRead>(`/v1/projects/${projectId}/agents/${name}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  validatePolicy: (
    name: string,
    policy_yaml: string,
    projectId = DEFAULT_PROJECT_ID,
  ) =>
    request<ValidatePolicyResponse>(
      `/v1/projects/${projectId}/agents/${name}/validate`,
      { method: 'POST', body: JSON.stringify({ policy_yaml }) },
    ),

  getAuditSummary: (scope: AuditScope = {}, projectId = DEFAULT_PROJECT_ID) =>
    request<AuditSummary>(
      `/v1/projects/${projectId}/audit/summary${qs({ ...scope })}`,
    ),

  getAuditTimeseries: (scope: AuditScope = {}, projectId = DEFAULT_PROJECT_ID) =>
    request<AuditTimeseriesPoint[]>(
      `/v1/projects/${projectId}/audit/timeseries${qs({ ...scope })}`,
    ),

  listAuditDecisions: (
    filters: AuditDecisionFilters = {},
    projectId = DEFAULT_PROJECT_ID,
  ) =>
    request<AuditDecisionPage>(
      `/v1/projects/${projectId}/audit/decisions${qs({ ...filters })}`,
    ),
}
