import { Route } from "react-router-dom";

import { AgentHub, AgentOverviewTab, AgentResourcesTab } from "./AgentHub";

/** The hub and its tabs, mounted by App and the hub tests alike. */
export function agentHubRoute() {
  return (
    <Route path="agents/:name" element={<AgentHub />}>
      <Route index element={<AgentOverviewTab />} />
      <Route path="resources" element={<AgentResourcesTab />} />
    </Route>
  );
}
