import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";
import { ArrowLeft, Mail, Plus, X } from "lucide-react";

import { ConfirmDialog } from "@/components/ConfirmDialog";
import { InviteMemberDialog } from "@/components/InviteMemberDialog";
import { RoleSelect } from "@/components/RoleSelect";
import { Alert, AlertDescription } from "@/components/ui/alert";
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
import { ApiError } from "@/lib/api";
import { useUser } from "@/lib/auth";
import {
  useInvitations,
  useRevokeInvitation,
  type InvitationRead,
} from "@/lib/invites";
import {
  useMembers,
  useRemoveMember,
  useUpdateMemberRole,
  type MemberRead,
} from "@/lib/members";
import { useOrgs, type Role } from "@/lib/orgs";

/**
 * `/orgs/:orgId/members` — two adjacent tables in one page.
 *
 *   - Top:    existing members, with inline role select + Remove
 *   - Bottom: pending invitations (admin/owner only), with Cancel
 *
 * Plus the "Invite member" button in the header. Plain members see a
 * read-only members list; the invitations section is hidden.
 *
 * Role-aware rendering reads `role` from useOrgs() rather than calling
 * `/users/me` + `/orgs/{id}/members` and computing — saves a round
 * trip on every render.
 */
export function OrgMembersPage() {
  const { orgId } = useParams<{ orgId: string }>();
  const navigate = useNavigate();
  const { user: me } = useUser();
  const orgsQuery = useOrgs();
  const membersQuery = useMembers(orgId ?? null);
  const invitationsQuery = useInvitations(orgId ?? null);
  const updateRole = useUpdateMemberRole();
  const removeMember = useRemoveMember();
  const revokeInvite = useRevokeInvitation();

  const [inviteOpen, setInviteOpen] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState<MemberRead | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState<InvitationRead | null>(
    null,
  );

  const org = orgsQuery.data?.find((o) => o.id === orgId) ?? null;
  const canManage = org?.role === "owner" || org?.role === "admin";

  if (orgsQuery.isLoading) {
    return (
      <div className="mx-auto max-w-4xl p-6 text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }

  if (!org) {
    return (
      <div className="mx-auto max-w-4xl space-y-4 p-6">
        <Button variant="ghost" size="sm" onClick={() => navigate("/orgs")}>
          <ArrowLeft className="h-3.5 w-3.5" />
          Back to organizations
        </Button>
        <Alert variant="destructive">
          <AlertDescription>
            Organization not found, or you no longer have access.
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  async function onRoleChange(member: MemberRead, role: Role): Promise<void> {
    if (member.role === role || !org) return;
    try {
      await updateRole.mutateAsync({
        orgId: org.id,
        userId: member.user_id,
        role,
      });
      toast.success(`${member.email} is now ${role}`);
    } catch (err) {
      const detail = err instanceof ApiError ? String(err.message) : "";
      const lastOwner = detail.toLowerCase().includes("last owner");
      toast.error(
        lastOwner
          ? "Can't demote the only owner — promote someone else first."
          : `Could not update ${member.email}'s role.`,
      );
    }
  }

  async function onRemove(member: MemberRead): Promise<void> {
    if (!org) return;
    try {
      await removeMember.mutateAsync({
        orgId: org.id,
        userId: member.user_id,
      });
      toast.success(`Removed ${member.email}`);
    } catch (err) {
      const detail = err instanceof ApiError ? String(err.message) : "";
      const lastOwner = detail.toLowerCase().includes("last owner");
      toast.error(
        lastOwner
          ? "Can't remove the only owner — promote someone else first."
          : `Could not remove ${member.email}.`,
      );
    } finally {
      setConfirmRemove(null);
    }
  }

  async function onRevokeInvite(invitation: InvitationRead): Promise<void> {
    if (!org) return;
    try {
      await revokeInvite.mutateAsync({
        invitationId: invitation.id,
        orgId: org.id,
      });
      toast.success(`Cancelled invite to ${invitation.email}`);
    } catch {
      toast.error(`Could not cancel invite to ${invitation.email}.`);
    } finally {
      setConfirmRevoke(null);
    }
  }

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <Button
        variant="ghost"
        size="sm"
        onClick={() => navigate(`/orgs/${org.id}/settings`)}
        className="-ml-2"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        Back to {org.name}
      </Button>

      <header className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            Members of {org.name}
          </h1>
          <p className="text-sm text-muted-foreground">
            Manage who can access this organization.
          </p>
        </div>
        {canManage && (
          <Button onClick={() => setInviteOpen(true)}>
            <Plus className="h-4 w-4" />
            Invite member
          </Button>
        )}
      </header>

      {/* ----- Members table ----- */}
      <section className="rounded-lg border border-border bg-card">
        <div className="border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold">
            Members ({membersQuery.data?.length ?? 0})
          </h2>
        </div>
        {membersQuery.isLoading && (
          <div className="p-6 text-sm text-muted-foreground">Loading…</div>
        )}
        {membersQuery.data && membersQuery.data.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Email</TableHead>
                <TableHead className="w-[140px]">Role</TableHead>
                {canManage && (
                  <TableHead className="w-[80px] text-right">Actions</TableHead>
                )}
              </TableRow>
            </TableHeader>
            <TableBody>
              {membersQuery.data.map((member) => {
                const isSelf = member.user_id === me?.id;
                return (
                  <TableRow key={member.user_id}>
                    <TableCell>
                      <span className="font-mono text-xs">{member.email}</span>
                      {isSelf && (
                        <span className="ml-2 text-[10px] uppercase tracking-wider text-muted-foreground">
                          you
                        </span>
                      )}
                    </TableCell>
                    <TableCell>
                      {canManage ? (
                        <RoleSelect
                          value={member.role}
                          onChange={(role) => onRoleChange(member, role)}
                          disabled={updateRole.isPending}
                        />
                      ) : (
                        <Badge
                          variant={
                            member.role === "owner" ? "primary" : "outline"
                          }
                          className="font-normal capitalize"
                        >
                          {member.role}
                        </Badge>
                      )}
                    </TableCell>
                    {canManage && (
                      <TableCell className="text-right">
                        {!isSelf && (
                          <Button
                            variant="ghost"
                            size="sm"
                            disabled={removeMember.isPending}
                            onClick={() => setConfirmRemove(member)}
                          >
                            <X className="h-3.5 w-3.5" />
                            Remove
                          </Button>
                        )}
                      </TableCell>
                    )}
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </section>

      {/* ----- Pending invitations table (admin/owner only) ----- */}
      {canManage && (
        <section className="rounded-lg border border-border bg-card">
          <div className="border-b border-border px-4 py-3">
            <h2 className="text-sm font-semibold">
              Pending invitations ({invitationsQuery.data?.length ?? 0})
            </h2>
          </div>
          {invitationsQuery.isLoading && (
            <div className="p-6 text-sm text-muted-foreground">Loading…</div>
          )}
          {invitationsQuery.data && invitationsQuery.data.length === 0 && (
            <div className="flex flex-col items-center gap-2 p-8 text-center">
              <Mail className="h-8 w-8 text-muted-foreground" />
              <p className="text-sm text-muted-foreground">
                No pending invitations. Invite a teammate to get started.
              </p>
            </div>
          )}
          {invitationsQuery.data && invitationsQuery.data.length > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Email</TableHead>
                  <TableHead className="w-[120px]">Role</TableHead>
                  <TableHead className="w-[160px]">Invited by</TableHead>
                  <TableHead className="w-[80px] text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {invitationsQuery.data.map((invitation) => (
                  <TableRow key={invitation.id}>
                    <TableCell>
                      <span className="font-mono text-xs">
                        {invitation.email}
                      </span>
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant="outline"
                        className="font-normal capitalize"
                      >
                        {invitation.role}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <span className="text-xs text-muted-foreground">
                        {invitation.invited_by_email}
                      </span>
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={revokeInvite.isPending}
                        onClick={() => setConfirmRevoke(invitation)}
                      >
                        <X className="h-3.5 w-3.5" />
                        Cancel
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </section>
      )}

      <InviteMemberDialog
        open={inviteOpen}
        onOpenChange={setInviteOpen}
        orgId={org.id}
      />

      <ConfirmDialog
        open={confirmRemove !== null}
        onOpenChange={(open) => !open && setConfirmRemove(null)}
        title={confirmRemove ? `Remove ${confirmRemove.email}?` : ""}
        description="They'll lose access to this organization's projects, tokens, and audit logs. An admin can re-invite them later."
        confirmLabel="Remove member"
        pending={removeMember.isPending}
        onConfirm={() =>
          confirmRemove ? onRemove(confirmRemove) : Promise.resolve()
        }
      />

      <ConfirmDialog
        open={confirmRevoke !== null}
        onOpenChange={(open) => !open && setConfirmRevoke(null)}
        title={confirmRevoke ? `Cancel invite to ${confirmRevoke.email}?` : ""}
        description="The magic link in their email will stop working. You can always send a fresh invite later."
        confirmLabel="Cancel invitation"
        pending={revokeInvite.isPending}
        onConfirm={() =>
          confirmRevoke ? onRevokeInvite(confirmRevoke) : Promise.resolve()
        }
      />
    </div>
  );
}
