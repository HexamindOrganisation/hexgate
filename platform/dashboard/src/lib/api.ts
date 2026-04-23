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
}
