import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Building2, ChevronRight, Plus } from "lucide-react";

import { CreateOrgDialog } from "@/components/CreateOrgDialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/ui/page-header";
import { useActive } from "@/lib/active";
import { useOrgs, type OrgWithRole, type Role } from "@/lib/orgs";

/**
 * `/orgs` — list every organization the caller belongs to with their
 * role on each row. Clicking a row navigates to the settings page for
 * that org AND sets it active in the switcher, so the rest of the
 * dashboard (Agents, Tokens, etc.) is now scoped to that org.
 *
 * "+ Create organization" opens the same dialog the switcher's
 * footer action does — single source of truth for the create form.
 */
export function OrgsPage() {
  const [createOpen, setCreateOpen] = useState(false);
  const orgsQuery = useOrgs();
  const setActiveOrg = useActive((s) => s.setActiveOrg);
  const navigate = useNavigate();

  function openOrg(org: OrgWithRole): void {
    // Setting active + navigating is one mental action; do both so
    // the user lands on the settings page WITH the active-org chrome
    // (switcher, sidebar) already in the right state.
    setActiveOrg(org.id);
    navigate(`/orgs/${org.id}/settings`);
  }

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <PageHeader
        title="Organizations"
        description="All organizations you belong to. Roles determine what you can change."
        actions={
          <Button onClick={() => setCreateOpen(true)}>
            <Plus className="size-4" />
            Create organization
          </Button>
        }
      />

      {orgsQuery.isError && (
        <div className="rounded-xl border border-border bg-card p-6 text-sm text-deny">
          Could not load organizations. Try refreshing the page.
        </div>
      )}

      {orgsQuery.isLoading && (
        <div className="rounded-xl border border-border bg-card p-6 text-sm text-muted-foreground">
          Loading…
        </div>
      )}

      {orgsQuery.data && orgsQuery.data.length === 0 && (
        <EmptyState onCreate={() => setCreateOpen(true)} />
      )}

      {orgsQuery.data && orgsQuery.data.length > 0 && (
        <div className="divide-y divide-border overflow-hidden rounded-xl border border-border bg-card">
          {orgsQuery.data.map((org) => (
            <button
              key={org.id}
              type="button"
              onClick={() => openOrg(org)}
              className="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-accent"
            >
              <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground">
                <Building2 className="size-4" />
              </span>
              <div className="min-w-0">
                <div className="truncate text-sm font-medium">{org.name}</div>
                <div className="truncate font-mono text-xs text-muted-foreground">
                  {org.slug}
                </div>
              </div>
              <RoleBadge role={org.role} />
              <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
            </button>
          ))}
        </div>
      )}

      <CreateOrgDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={(orgId) => {
          // The dialog already sets activeOrg; we just navigate.
          navigate(`/orgs/${orgId}/settings`);
        }}
      />
    </div>
  );
}

function EmptyState({ onCreate }: { onCreate: () => void }) {
  // The auto-create-default-org-on-signup means this state is unusual
  // — a user would have to deliberately leave their default org to
  // land here. Still possible (e.g., they joined another org and left
  // their personal one), so render a useful zero-state.
  return (
    <div className="flex flex-col items-center gap-3 rounded-xl border border-border bg-card p-10 text-center">
      <span className="grid size-11 place-items-center rounded-xl bg-action text-action-foreground">
        <Building2 className="size-5" />
      </span>
      <h2 className="text-base font-medium">No organizations yet</h2>
      <p className="max-w-md text-sm text-muted-foreground">
        Create one to start a workspace. You'll be the owner — you can invite
        teammates afterwards.
      </p>
      <Button onClick={onCreate} className="mt-1">
        <Plus className="size-4" />
        Create organization
      </Button>
    </div>
  );
}

function RoleBadge({ role }: { role: Role }) {
  // Owners get the primary tint (load-bearing role); admins the neutral
  // default; members the outline-only.
  const variant: Record<Role, "primary" | "default" | "outline"> = {
    owner: "primary",
    admin: "default",
    member: "outline",
  };
  return (
    <Badge
      variant={variant[role]}
      className="ml-auto shrink-0 font-normal capitalize"
    >
      {role}
    </Badge>
  );
}
