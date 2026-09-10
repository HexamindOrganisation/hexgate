import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { FileCode } from "lucide-react";

import { useProjectScoped } from "@/lib/active";
import { useResolvedPolicy } from "@/lib/policy_files";
import { PolicyGraphView } from "@/components/policy_files/PolicyGraphDialog";
import { NoProjectEmptyState } from "@/components/NoProjectEmptyState";

/**
 * Full-page policy graph — the same resource-policy graph the Policies editor
 * opens in a dialog (agents, tools, MCP, and the reach/admission edges between
 * them), given its own route so it can be explored full-screen. "Edit policies"
 * jumps to the files editor.
 */
export function GraphPage() {
  const scope = useProjectScoped();
  const projectId = scope.projectId;
  const navigate = useNavigate();

  // Roles for the filter come from resolving the project (compose has no
  // separate role registry); a classic project 422s → no roles, and the graph
  // shows its empty/error state.
  const resolved = useResolvedPolicy(projectId, undefined, undefined);
  const roleNames = useMemo(
    () => Object.keys(resolved.data ?? {}).sort(),
    [resolved.data],
  );

  if (scope.status === "no-project") {
    return <NoProjectEmptyState resource="graph" />;
  }
  if (!projectId) {
    return (
      <div className="grid h-full place-items-center text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }

  return (
    <div className="-mx-8 -my-6 h-screen overflow-hidden">
      <PolicyGraphView
        projectId={projectId}
        roleNames={roleNames}
        headerRight={
          <button
            type="button"
            onClick={() => navigate("/policies")}
            title="Edit the policy files"
            className="inline-flex items-center gap-1.5 rounded-md border border-white/15 px-2 py-1 text-[11px] font-medium text-[#cfd6e4] transition-colors hover:bg-white/5"
          >
            <FileCode size={12} />
            Edit policies
          </button>
        }
      />
    </div>
  );
}
