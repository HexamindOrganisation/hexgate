import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { toast } from "sonner";
import { AlertTriangle, ArrowLeft, LogOut, Users } from "lucide-react";

import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { PageHeader } from "@/components/ui/page-header";
import { ApiError } from "@/lib/api";
import { useActive } from "@/lib/active";
import { useUser } from "@/lib/auth";
import { useLeaveOrg } from "@/lib/members";
import { useOrgs, useUpdateOrg } from "@/lib/orgs";

/** Mirror of CreateOrgDialog's slug regex (DNS-label shape). */
const SLUG_REGEX = /^([a-z]|[a-z][a-z0-9-]*[a-z0-9])$/;

const SettingsSchema = z.object({
  name: z.string().min(1, "Required").max(64, "Max 64 characters"),
  slug: z
    .string()
    .min(1, "Required")
    .max(32, "Max 32 characters")
    .regex(
      SLUG_REGEX,
      "Lowercase letters, digits, hyphens. Must start with a letter.",
    ),
});

type SettingsValues = z.infer<typeof SettingsSchema>;

/**
 * `/orgs/:orgId/settings` — name + slug editing + the "Leave organization"
 * danger zone.
 *
 * Role-aware:
 *   - Plain members see read-only inputs + an explanatory line
 *   - Admin/owner can edit + save
 *   - "Leave" is enabled regardless of role; the backend's
 *     last-owner guard returns 409 which we surface as a friendly
 *     toast ("you're the only owner; promote someone first")
 */
export function OrgSettingsPage() {
  const { orgId } = useParams<{ orgId: string }>();
  const navigate = useNavigate();
  const { user } = useUser();
  const orgsQuery = useOrgs();
  const updateOrg = useUpdateOrg();
  const leaveOrg = useLeaveOrg();
  const setActiveOrg = useActive((s) => s.setActiveOrg);
  const [confirmLeaveOpen, setConfirmLeaveOpen] = useState(false);

  const org = orgsQuery.data?.find((o) => o.id === orgId) ?? null;
  const canEdit = org?.role === "owner" || org?.role === "admin";

  const form = useForm<SettingsValues>({
    resolver: zodResolver(SettingsSchema),
    defaultValues: { name: "", slug: "" },
  });

  // Reset the form whenever the active org row changes — switching orgs
  // via the switcher while sitting on this page should re-populate the
  // fields with the new org's values.
  useEffect(() => {
    if (org) {
      form.reset({ name: org.name, slug: org.slug });
      updateOrg.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [org?.id, org?.name, org?.slug]);

  if (orgsQuery.isLoading) {
    return (
      <div className="mx-auto max-w-3xl p-6 text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }

  if (!org) {
    // Either the org doesn't exist or the user isn't a member.
    // Backend would 403/404 the underlying API; here we surface the
    // friendlier message without an API round-trip.
    return (
      <div className="mx-auto max-w-3xl space-y-4 p-6">
        <BackToOrgs onClick={() => navigate("/orgs")} />
        <Alert variant="destructive">
          <AlertDescription>
            Organization not found, or you no longer have access to it.
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  async function onSubmit(values: SettingsValues): Promise<void> {
    if (!org) return; // narrows the type below
    try {
      await updateOrg.mutateAsync({
        orgId: org.id,
        name: values.name !== org.name ? values.name : undefined,
        slug: values.slug !== org.slug ? values.slug : undefined,
      });
      toast.success("Organization saved");
    } catch (err) {
      const detail = err instanceof ApiError ? String(err.message) : "";
      const slugTaken = detail.toLowerCase().includes("taken");
      toast.error(
        slugTaken
          ? "That slug is already in use. Try a different one."
          : "Could not save changes.",
      );
    }
  }

  async function onLeave(): Promise<void> {
    if (!user || !org) return;
    try {
      await leaveOrg.mutateAsync({ orgId: org.id, userId: user.id });
      toast.success(`Left "${org.name}"`);
      // Drop active-org so the bootstrap in AppShell picks a fresh one.
      setActiveOrg(null);
      navigate("/orgs", { replace: true });
    } catch (err) {
      const detail = err instanceof ApiError ? String(err.message) : "";
      const lastOwner = detail.toLowerCase().includes("last owner");
      toast.error(
        lastOwner
          ? "You're the only owner — promote someone to owner before leaving."
          : "Could not leave the organization.",
      );
    } finally {
      setConfirmLeaveOpen(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <BackToOrgs onClick={() => navigate("/orgs")} />

      <PageHeader
        title="Organization settings"
        description={
          <>
            <span className="font-medium text-foreground">{org.name}</span>
            <span> · </span>
            <span className="font-mono">{org.slug}</span>
            <span> · your role: </span>
            <span className="capitalize text-foreground">{org.role}</span>
          </>
        }
      />

      <form
        onSubmit={form.handleSubmit(onSubmit)}
        id="org-settings-form"
        className="rounded-xl border border-border bg-card"
      >
        <div className="space-y-4 p-5">
          {!canEdit && (
            <Alert>
              <AlertDescription>
                View-only — only admins and owners can edit this organization's
                name or slug.
              </AlertDescription>
            </Alert>
          )}

          <div className="space-y-2">
            <Label htmlFor="org-name">Name</Label>
            <Input
              id="org-name"
              disabled={!canEdit}
              autoComplete="off"
              {...form.register("name")}
            />
            {form.formState.errors.name && (
              <p className="text-xs text-deny">
                {form.formState.errors.name.message}
              </p>
            )}
          </div>

          <div className="space-y-2">
            <Label htmlFor="org-slug">Slug</Label>
            <Input
              id="org-slug"
              disabled={!canEdit}
              autoComplete="off"
              {...form.register("slug")}
            />
            <p className="text-xs text-muted-foreground">
              Used in URLs. Changing this breaks existing bookmarks.
            </p>
            {form.formState.errors.slug && (
              <p className="text-xs text-deny">
                {form.formState.errors.slug.message}
              </p>
            )}
          </div>
        </div>

        {canEdit && (
          <div className="flex justify-end border-t border-border px-5 py-3">
            <Button
              type="submit"
              disabled={updateOrg.isPending || !form.formState.isDirty}
            >
              {updateOrg.isPending ? "Saving…" : "Save changes"}
            </Button>
          </div>
        )}
      </form>

      <section className="flex items-center gap-4 rounded-xl border border-border bg-card p-5">
        <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground">
          <Users className="size-4" />
        </span>
        <div className="min-w-0">
          <h2 className="text-sm font-medium">Members</h2>
          <p className="text-xs text-muted-foreground">
            See who has access, invite teammates, change roles, or remove
            members.
          </p>
        </div>
        <Button
          variant="outline"
          className="ml-auto shrink-0"
          onClick={() => navigate(`/orgs/${org.id}/members`)}
        >
          Manage members
        </Button>
      </section>

      <section className="rounded-xl border border-deny/40 bg-card">
        <div className="flex items-start gap-4 p-5">
          <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-deny/10 text-deny">
            <AlertTriangle className="size-4" />
          </span>
          <div className="min-w-0">
            <h2 className="text-sm font-medium">Danger zone</h2>
            <p className="text-xs text-muted-foreground">
              Leaving revokes every API key you own in this organization and
              removes your access to its projects. You can't undo this —
              re-joining requires an invite, and revoked keys can't be restored.
            </p>
          </div>
        </div>
        <div className="flex justify-end border-t border-deny/30 px-5 py-3">
          <Button
            variant="outline"
            className="gap-1.5 border-deny/40 text-deny hover:bg-deny/10 hover:text-deny"
            onClick={() => setConfirmLeaveOpen(true)}
            disabled={leaveOrg.isPending}
          >
            <LogOut className="size-4" />
            Leave organization
          </Button>
        </div>
      </section>

      <ConfirmDialog
        open={confirmLeaveOpen}
        onOpenChange={setConfirmLeaveOpen}
        title={`Leave ${org.name}?`}
        description={`Any API keys you own in ${org.name} are revoked immediately — services still using them will start failing. You'll also lose access to its projects and audit logs. An admin can re-invite you later, but revoked keys can't be restored.`}
        confirmLabel="Leave organization"
        confirmVariant="destructive"
        pending={leaveOrg.isPending}
        onConfirm={onLeave}
      />
    </div>
  );
}

function BackToOrgs({ onClick }: { onClick: () => void }) {
  return (
    <Button variant="ghost" size="sm" onClick={onClick} className="-ml-2">
      <ArrowLeft className="size-3.5" />
      Back to organizations
    </Button>
  );
}
