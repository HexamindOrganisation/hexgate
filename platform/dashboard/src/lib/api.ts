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
