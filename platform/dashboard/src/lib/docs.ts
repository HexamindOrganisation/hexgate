export const DOCS_BASE_URL = "https://docs.hexgate.ai";

export const DOC_PATHS = {
  tokens: "/platform/workflow",
  policies: "/policy/yaml-shape",
  playground: "/cli/serve",
  registerAgent: "/cli/register",
  quickstart: "/quickstart",
} as const;

export function docsUrl(path: string): string {
  return `${DOCS_BASE_URL}${path}`;
}

/** Opens the API keys page with its "Mint API key" dialog already open. */
export function mintKeyPath(name?: string): string {
  const q = new URLSearchParams({ mint: "1" });
  if (name) q.set("name", name);
  return `/tokens?${q.toString()}`;
}
