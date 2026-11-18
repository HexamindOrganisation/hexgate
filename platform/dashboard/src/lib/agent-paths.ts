/** The hub page of agent `name` (the `agents/:name` route in
 * routes/agent-hub-routes.tsx). */
export const agentHubPath = (name: string) =>
  `/agents/${encodeURIComponent(name)}`;

/** Past this many agents, the sidebar and Home cap their lists and /agents
 * gets a name filter: one threshold, so "View all" always lands on a
 * filterable list. The sidebar keeps manifest order; Home ranks by activity. */
export const AGENT_LIST_CAP = 8;
