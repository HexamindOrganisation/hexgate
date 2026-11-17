/**
 * The Policies page's Graph button: graphs are per agent, so it asks which
 * agent and opens that agent's hub.
 */

import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Search } from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { agentHubPath } from "@/lib/agent-paths";
import { pluralise } from "@/lib/format";

export function AgentGraphPicker({
  agents,
  onClose,
}: {
  /** Agents the policy names. */
  agents: string[];
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? agents.filter((a) => a.toLowerCase().includes(q)) : agents;
  }, [agents, query]);
  const open = (name: string) => {
    onClose();
    navigate(agentHubPath(name));
  };
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="flex max-h-[80vh] max-w-[440px] flex-col gap-0 overflow-hidden p-0">
        <div className="space-y-1.5 px-5 pb-3 pt-5">
          <DialogTitle>Open an agent's graph</DialogTitle>
          <DialogDescription>
            Each agent has its graph on its own page: its roles, tools and the
            sub-agents it reaches.
          </DialogDescription>
        </div>
        {agents.length > 0 ? (
          <>
            <form
              role="search"
              className="px-5 pb-3"
              onSubmit={(e) => {
                e.preventDefault();
                if (shown[0]) open(shown[0]);
              }}
            >
              <label className="flex h-10 items-center gap-2 rounded-lg border border-input px-3 focus-within:ring-2 focus-within:ring-ring">
                <Search
                  className="size-3.5 text-muted-foreground"
                  aria-hidden
                />
                <input
                  type="search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder={`Search ${pluralise(agents.length, "agent")}`}
                  aria-label="Search agents"
                  className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
                />
              </label>
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
            The policy names no agents yet. Open one from{" "}
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
