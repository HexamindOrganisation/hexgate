/** The hub page of agent `name` (the `agents/:name` route in
 * routes/agent-hub-routes.tsx). */
export const agentHubPath = (name: string) =>
  `/agents/${encodeURIComponent(name)}`;
