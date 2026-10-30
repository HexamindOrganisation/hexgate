import { Route } from "react-router-dom";

import {
  AgentDecisionsTab,
  AgentHub,
  AgentOverviewTab,
  AgentPlaygroundTab,
  AgentPoliciesTab,
  AgentResourcesTab,
} from "./AgentHub";

/** The hub and its tabs, mounted by App and the hub tests alike. */
export function agentHubRoute() {
  return (
    <Route path="agents/:name" element={<AgentHub />}>
      <Route index element={<AgentOverviewTab />} />
      <Route path="resources" element={<AgentResourcesTab />} />
      <Route path="policies" element={<AgentPoliciesTab />} />
      <Route path="playground" element={<AgentPlaygroundTab />} />
      <Route path="decisions" element={<AgentDecisionsTab />} />
    </Route>
  );
}
