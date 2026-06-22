import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Building2, Plus, ArrowRight } from "lucide-react";

import { CreateOrgDialog } from "@/components/CreateOrgDialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
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
      <header className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            Organizations
          </h1>
          <p className="text-sm text-muted-foreground">
            All organizations you belong to. Roles determine what you can
            change.
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" />
          Create organization
        </Button>
      </header>

      <div className="rounded-lg border border-border bg-card">
        {orgsQuery.isLoading && (
          <div className="p-6 text-sm text-muted-foreground">Loading…</div>
        )}
        {orgsQuery.isError && (
          <div className="p-6 text-sm text-destructive">
            Could not load organizations. Try refreshing the page.
          </div>
        )}
        {orgsQuery.data && orgsQuery.data.length === 0 && (
          <EmptyState onCreate={() => setCreateOpen(true)} />
        )}
        {orgsQuery.data && orgsQuery.data.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Slug</TableHead>
                <TableHead>Role</TableHead>
                <TableHead className="w-[100px] text-right" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {orgsQuery.data.map((org) => (
                <TableRow
                  key={org.id}
                  className="cursor-pointer"
                  onClick={() => openOrg(org)}
                >
                  <TableCell>
                    <div className="flex items-center gap-2 font-medium">
                      <Building2 className="h-4 w-4 text-primary" />
                      {org.name}
                    </div>
                  </TableCell>
                  <TableCell>
                    <span className="font-mono text-xs text-muted-foreground">
                      {org.slug}
                    </span>
                  </TableCell>
                  <TableCell>
                    <RoleBadge role={org.role} />
                  </TableCell>
                  <TableCell className="text-right">
                    <Button asChild variant="ghost" size="sm">
                      <Link
                        to={`/orgs/${org.id}/settings`}
                        onClick={(e) => {
                          // Stop the row's onClick from also firing.
                          e.stopPropagation();
                          setActiveOrg(org.id);
                        }}
                      >
                        Open
                        <ArrowRight className="h-3.5 w-3.5" />
                      </Link>
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>

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
    <div className="flex flex-col items-center gap-3 p-10 text-center">
      <Building2 className="h-10 w-10 text-muted-foreground" />
      <h2 className="text-lg font-medium">No organizations yet</h2>
      <p className="max-w-md text-sm text-muted-foreground">
        Create one to start a workspace. You'll be the owner — you can invite
        teammates afterwards.
      </p>
      <Button onClick={onCreate} className="mt-2">
        <Plus className="h-4 w-4" />
        Create organization
      </Button>
    </div>
  );
}

function RoleBadge({ role }: { role: Role }) {
  // Reuse the existing dashboard Badge palette. Owners get the
  // primary tint (load-bearing role); admins the neutral default
  // (the secondary in shadcn parlance), members the outline-only.
  const variant: Record<Role, "primary" | "default" | "outline"> = {
    owner: "primary",
    admin: "default",
    member: "outline",
  };
  return (
    <Badge variant={variant[role]} className="font-normal capitalize">
      {role}
    </Badge>
  );
}
