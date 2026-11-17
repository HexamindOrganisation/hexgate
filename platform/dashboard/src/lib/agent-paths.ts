/** The hub page of agent `name` (the `agents/:name` route in
 * routes/agent-hub-routes.tsx). */
export const agentHubPath = (name: string) =>
  `/agents/${encodeURIComponent(name)}`;

/** Past this many agents, the sidebar caps its list and /agents gets a name
 * filter: one threshold, so "View all" always lands on a filterable list. */
export const AGENT_LIST_CAP = 8;
