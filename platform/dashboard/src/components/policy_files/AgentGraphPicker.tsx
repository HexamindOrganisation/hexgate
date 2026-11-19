/**
 * The Policies page's Graph button: graphs are per agent, so it asks which
 * agent and opens that agent's hub.
 */

import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { SearchField } from "@/components/ui/search-field";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";

import { matchesQuery, normalizeQuery } from "@/lib/search";
import { useAgentManifests } from "@/lib/agent-manifests";
import { agentHubPath } from "@/lib/agent-paths";
import { pluralise } from "@/lib/format";

export function AgentGraphPicker({
  projectId,
  newTab = false,
  onClose,
}: {
  projectId: string;
  /** Open the hub in a new tab, so the page's unsaved edits survive. */
  newTab?: boolean;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  // The project's agents, each with a hub: not `*`, nor a name only an
  // unsaved draft adds.
  const manifests = useAgentManifests(projectId);
  const agents = useMemo(
    () => (manifests.data ?? []).map((m) => m.name).sort(),
    [manifests.data],
  );
  const [query, setQuery] = useState("");
  const shown = useMemo(() => {
    const q = normalizeQuery(query);
    return q ? agents.filter((a) => matchesQuery(a, q)) : agents;
  }, [agents, query]);
  const open = (name: string) => {
    onClose();
    if (newTab) window.open(agentHubPath(name), "_blank", "noopener");
    else navigate(agentHubPath(name));
  };
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="flex max-h-[80vh] max-w-[440px] flex-col gap-0 overflow-hidden p-0">
        <div className="space-y-1.5 px-5 pb-3 pt-5">
          <DialogTitle>Open an agent's graph</DialogTitle>
          <DialogDescription>
            Each agent has its graph on its own page: its roles, tools and the
            sub-agents it reaches.
            {newTab &&
              " You have unsaved edits, so it opens in a new tab; it shows the saved policy."}
          </DialogDescription>
        </div>
        {manifests.isPending ? (
          <p className="px-5 pb-5 text-sm text-muted-foreground">
            Loading agents…
          </p>
        ) : manifests.isError ? (
          <p className="px-5 pb-5 text-sm text-muted-foreground">
            Couldn't load agents. Try again.
          </p>
        ) : agents.length > 0 ? (
          <>
            <form
              role="search"
              className="px-5 pb-3"
              onSubmit={(e) => {
                e.preventDefault();
                if (shown[0]) open(shown[0]);
              }}
            >
              <SearchField
                value={query}
                onChange={setQuery}
                placeholder={`Search ${pluralise(agents.length, "agent")}`}
                label="Search agents"
              />
            </form>
            <ul className="min-h-0 flex-1 overflow-y-auto border-t border-border py-1">
              {shown.map((a) => (
                <li key={a}>
                  <button
                    type="button"
                    onClick={() => open(a)}
                    className="flex min-h-11 w-full items-center px-5 text-left font-mono text-[13px] hover:bg-accent/60"
                  >
                    {a}
                  </button>
                </li>
              ))}
              {shown.length === 0 && (
                <li className="px-5 py-3 text-sm text-muted-foreground">
                  No agent matches “{query.trim()}”.
                </li>
              )}
            </ul>
          </>
        ) : (
          <p className="px-5 pb-5 text-sm text-muted-foreground">
            No agents in this project yet. See{" "}
            <Link to="/agents" onClick={onClose} className="text-primary">
              Agents
            </Link>
            .
          </p>
        )}
      </DialogContent>
    </Dialog>
  );
}
