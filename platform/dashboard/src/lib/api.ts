/** Routes that are reachable without a session cookie — never redirect
 * away from these on a 401, otherwise we'd bounce the sign-in form itself
 * back to /sign-in in a loop when the user has bad credentials. */
const PUBLIC_AUTH_PATHS = [
  '/sign-in',
  '/sign-up',
  '/forgot-password',
  '/reset-password',
  '/verify-email',
]

/** Thrown by ``request`` when the backend returns 401, after the global
 * "redirect to /sign-in" side-effect has fired. Tests assert against the
 * error type; production code rarely catches it (the redirect already
 * happened). */
export class UnauthenticatedError extends Error {
  constructor(message = 'not authenticated') {
    super(message)
    this.name = 'UnauthenticatedError'
  }
}

/** Thrown for any non-2xx response other than 401 — carries the parsed
 * detail when the backend returns one (FastAPI Users speaks JSON with
 * a ``detail`` field). Fields are declared explicitly (not via TS
 * constructor parameter properties) because the project's tsconfig
 * enables ``erasableSyntaxOnly``. */
export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, detail: unknown, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    // ``include`` so the fortify_session cookie rides on cross-origin
    // dev (vite on 5173 → api on 8000) and on prod where the dashboard
    // and API may live on different subdomains.
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  })
  if (res.status === 401) {
    // Single global redirect — every component that does a request gets
    // sent to sign-in if their session expired. Public auth routes are
    // exempt: a 401 there is just "wrong password", surface it normally.
    const onAuthPage = PUBLIC_AUTH_PATHS.some((p) =>
      window.location.pathname.startsWith(p),
    )
    if (!onAuthPage) {
      window.location.href = '/sign-in'
    }
    throw new UnauthenticatedError()
  }
  if (!res.ok) {
    let detail: unknown
    let bodyText = ''
    try {
      bodyText = await res.text()
      detail = bodyText ? JSON.parse(bodyText) : null
    } catch {
      detail = bodyText
    }
    const message =
      typeof detail === 'object' && detail !== null && 'detail' in detail
        ? String((detail as { detail: unknown }).detail)
        : `${res.status} ${res.statusText}`
    throw new ApiError(res.status, detail, message)
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
      method: 'POST',
      body: JSON.stringify(body),
    }),

  revokeToken: (tokenId: string, projectId: string) =>
    request<void>(`/v1/projects/${projectId}/tokens/${tokenId}`, {
      method: 'DELETE',
    }),

  listAgents: (projectId: string) =>
    request<AgentRead[]>(`/v1/projects/${projectId}/agents`),

  listAgentManifests: (projectId: string) =>
    request<AgentManifestView[]>(
      `/v1/projects/${projectId}/agents/manifest`,
    ),

  getAgent: (name: string, projectId: string) =>
    request<AgentRead>(`/v1/projects/${projectId}/agents/${name}`),

  updateAgent: (name: string, body: AgentUpdate, projectId: string) =>
    request<AgentRead>(`/v1/projects/${projectId}/agents/${name}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  validatePolicy: (
    name: string,
    policy_yaml: string,
    projectId: string,
  ) =>
    request<ValidatePolicyResponse>(
      `/v1/projects/${projectId}/agents/${name}/validate`,
      { method: 'POST', body: JSON.stringify({ policy_yaml }) },
    ),
}
