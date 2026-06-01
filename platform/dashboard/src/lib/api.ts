// Matches services.DEFAULT_PROJECT_ID on the backend — the fixed UUID the
// triple-default seed populates on first boot. Will be replaced by the active
// org's project list once Phase 5 (org switcher + project list) lands.
export const DEFAULT_PROJECT_ID = '00000000-0000-0000-0000-000000000003'

// M3 Phase 2: the dashboard authenticates by sending the default seed user's
// UUID as an X-Dev-User header. Phase 3 replaces this with a session cookie
// issued by FastAPI Users after real sign-in; until then, every dashboard
// request rides on the default-admin identity.
const DEV_USER_ID = '00000000-0000-0000-0000-000000000002'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      'X-Dev-User': DEV_USER_ID,
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
}
