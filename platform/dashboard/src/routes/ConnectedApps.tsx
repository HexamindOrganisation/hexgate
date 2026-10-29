import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderGit2, Plug, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { formatAbsolute, formatRelative } from "@/components/bans/format";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ApiError, oauthGrantsApi, type OAuthGrant } from "@/lib/api";

const GRANTS_KEY = ["oauth-grants"] as const;

/** A revoke stops the refresh token only: an access token already issued is a
 * signed JWT nobody checks against the login, so it stays valid until it
 * expires. The figure is ACCESS_TOKEN_TTL_SECONDS (600) in platform/api's
 * core/oauth_tokens.py; change both together. */
const ACCESS_ENDS = "Access ends within 10 minutes.";

/** The host the client's metadata lives on: unlike ``client_name``, which the
 * client writes about itself, this is where it actually comes from. The
 * hostname without the port, as the consent page showed it (consent-info's
 * ``client_host`` in platform/api's oauth router). */
function clientHost(clientId: string): string {
  try {
    return new URL(clientId).hostname;
  } catch {
    return clientId;
  }
}

function useRevokeGrant() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (grantId: string) => {
      try {
        await oauthGrantsApi.revoke(grantId);
      } catch (err) {
        // 404: the login is already gone (revoked in another tab, or the
        // client logged itself out), which is what the user asked for.
        if (!(err instanceof ApiError && err.status === 404)) throw err;
      }
    },
    // Refetch on failure too, so the list shows what the server holds.
    onSettled: () => qc.invalidateQueries({ queryKey: GRANTS_KEY }),
  });
}

function GrantRow({
  grant,
  onRevoke,
}: {
  grant: OAuthGrant;
  onRevoke: () => void;
}) {
  return (
    <li className="space-y-3 px-5 py-4">
      <div className="flex items-start justify-between gap-4">
        {/* The client writes its own name: however long, it wraps rather
            than push Revoke out of the card. */}
        <div className="min-w-0 break-words">
          <div className="text-sm font-medium">{grant.client_name}</div>
          <div className="font-mono text-xs text-muted-foreground">
            {clientHost(grant.client_id)}
          </div>
        </div>
        <Button
          variant="ghost"
          size="sm"
          className="shrink-0 gap-1.5 text-xs text-destructive hover:bg-destructive/10 hover:text-destructive"
          onClick={onRevoke}
        >
          <Trash2 className="size-3.5" />
          Revoke
        </Button>
      </div>

      <ul className="list-disc space-y-0.5 pl-5 text-sm text-muted-foreground">
        {grant.scopes.map((s) => (
          <li key={s.name} title={s.name}>
            {s.description}
          </li>
        ))}
      </ul>

      <div className="flex flex-wrap items-center gap-1.5">
        <FolderGit2 className="size-3.5 text-muted-foreground" />
        {grant.projects.length === 0 ? (
          <span className="text-xs text-muted-foreground">
            No projects left: every project it covered was deleted.
          </span>
        ) : (
          grant.projects.map((p) => (
            <Badge key={p.id} title={p.id}>
              {p.org_name} / {p.name}
            </Badge>
          ))
        )}
      </div>

      <div className="text-xs text-muted-foreground">
        <span title={formatAbsolute(grant.created_at)}>
          Connected {formatRelative(grant.created_at)}
        </span>
        {/* Set when the client renews its access, not on every call it
            makes, so nothing is claimed about use before the first one. */}
        {grant.last_used_at && (
          <span title={formatAbsolute(grant.last_used_at)}>
            {" · "}Access last renewed {formatRelative(grant.last_used_at)}
          </span>
        )}
      </div>
    </li>
  );
}

/**
 * /connected-apps — the AI clients (MCP clients) that act for the signed-in
 * user, each with the projects its login covers, so a project ticked by
 * mistake at consent is noticed here. Revoke logs that client out: its
 * refresh token stops working and it must ask for consent again. Access
 * tokens are not revocable one by one, so one already issued lives out its
 * TTL, which ACCESS_ENDS states.
 *
 * Per user, not per project: it reads no project scope.
 */
export function ConnectedAppsPage() {
  const grantsQuery = useQuery({
    queryKey: GRANTS_KEY,
    queryFn: oauthGrantsApi.list,
  });
  const revokeGrant = useRevokeGrant();
  const [confirmRevoke, setConfirmRevoke] = useState<OAuthGrant | null>(null);

  async function revoke() {
    if (!confirmRevoke) return;
    try {
      await revokeGrant.mutateAsync(confirmRevoke.id);
      toast.success(
        `${confirmRevoke.client_name} was logged out. ${ACCESS_ENDS}`,
      );
      setConfirmRevoke(null);
    } catch {
      toast.error(`Could not log out ${confirmRevoke.client_name}.`);
    }
  }

  const grants = grantsQuery.data ?? [];

  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">
          Connected apps
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          AI clients you have let act for you, and the projects each one can
          reach. Revoke one to log it out.
        </p>
      </header>

      <div className="overflow-hidden rounded-lg border border-border bg-card">
        {grantsQuery.isLoading ? (
          <div className="p-12 text-center text-sm text-muted-foreground">
            Loading…
          </div>
        ) : grantsQuery.isError ? (
          <div className="flex flex-col items-center gap-3 p-12 text-center text-sm">
            <div className="text-destructive">
              Could not load your connected apps.
            </div>
            <Button variant="outline" onClick={() => grantsQuery.refetch()}>
              Try again
            </Button>
          </div>
        ) : grants.length === 0 ? (
          <div className="flex flex-col items-center gap-3 py-16 text-center">
            <Plug className="size-12 text-muted-foreground/40" />
            <div className="text-sm font-medium">No connected apps</div>
            <div className="max-w-xs text-xs text-muted-foreground">
              When you approve an AI client such as Claude Code, it shows up
              here.
            </div>
          </div>
        ) : (
          <ul className="divide-y divide-border">
            {grants.map((g) => (
              <GrantRow
                key={g.id}
                grant={g}
                onRevoke={() => setConfirmRevoke(g)}
              />
            ))}
          </ul>
        )}
      </div>

      <ConfirmDialog
        open={!!confirmRevoke}
        onOpenChange={(o) => !o && setConfirmRevoke(null)}
        title={`Revoke ${confirmRevoke?.client_name ?? ""}?`}
        description={`It can no longer renew its access and must ask you again to act for you. ${ACCESS_ENDS}`}
        confirmLabel="Revoke"
        onConfirm={revoke}
        pending={revokeGrant.isPending}
      />
    </div>
  );
}
