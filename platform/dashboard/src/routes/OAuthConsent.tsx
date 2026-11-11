import { useEffect, useState } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";
import { useLocation } from "react-router-dom";
import { Check } from "lucide-react";

import { AuthShell } from "@/components/auth/AuthShell";
import { Button } from "@/components/ui/button";
import { oauthApi, UnauthenticatedError, type ConsentInfo } from "@/lib/api";
import { USER_QUERY_KEY } from "@/lib/auth";
import {
  denyRedirect,
  hasRepeatedParams,
  isLeavableUrl,
  leaveFor,
  redirectUri,
} from "@/lib/oauth_consent";

/**
 * Where an MCP client's login lands: the user sees which app asks for which
 * scopes, ticks the projects it may reach, and approves or denies. The query
 * string is the client's authorization request; the server checks it again
 * (consent-info) before anything here trusts it.
 */
export function OAuthConsentPage() {
  const { search } = useLocation();
  const info = useQuery({
    queryKey: ["oauth-consent-info", search],
    queryFn: () => oauthApi.consentInfo(search),
    // A refused request stays refused: show the error at once.
    retry: false,
  });
  useSignInAgainOn(info.error);
  const problem = blockingProblem(info, search);

  if (problem) {
    return (
      <AuthShell
        tone="error"
        title="Can't authorize this request"
        description={<span role="alert">{problem}</span>}
      />
    );
  }
  if (!info.isSuccess) {
    return <AuthShell tone="muted" title="Checking this request…" />;
  }
  return (
    <AuthShell wide title="Authorize access to Hexgate">
      <ConsentForm info={info.data} search={search} />
    </AuthShell>
  );
}

/** A session lost while the page is open: forget the cached user, so
 * ProtectedRoute sends them to sign in with this page's full address (the
 * MCP client's request) and back here. */
function useSignInAgainOn(error: Error | null) {
  const qc = useQueryClient();
  useEffect(() => {
    if (error instanceof UnauthenticatedError) {
      qc.setQueryData(USER_QUERY_KEY, null);
    }
  }, [error, qc]);
}

/** Why the page offers neither Approve nor Deny, or null: an unchecked
 * request (its redirect_uri may be anything), a link whose parameters the
 * page and server could read differently, or a redirect it will not open. */
function blockingProblem(
  info: UseQueryResult<ConsentInfo>,
  search: string,
): string | null {
  // A lost session: useSignInAgainOn is already sending them to sign in.
  if (info.error instanceof UnauthenticatedError) return null;
  if (info.isError) {
    return `Could not check this authorization request: ${info.error.message}`;
  }
  if (!info.isSuccess) return null;
  if (hasRepeatedParams(search)) {
    return "This authorization link has been altered: a parameter appears twice.";
  }
  if (!isLeavableUrl(redirectUri(search))) {
    return "This application asks to be sent back to an address the dashboard will not open.";
  }
  return null;
}

function ConsentForm({ info, search }: { info: ConsentInfo; search: string }) {
  const [ticked, setTicked] = useState<ReadonlySet<string>>(new Set());
  const approve = useMutation({
    mutationFn: () => oauthApi.consent(search, [...ticked]),
    onSuccess: ({ redirect_to }) => leaveFor(redirect_to),
  });
  useSignInAgainOn(approve.error);

  function toggle(id: string) {
    setTicked((prev) => {
      const next = new Set(prev);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }

  return (
    <div className="space-y-5">
      <ClientIdentity info={info} />
      <section className="space-y-2">
        <h2 className="text-meta font-medium uppercase tracking-wider text-muted-foreground">
          It asks to
        </h2>
        <ul className="space-y-1.5 text-sm">
          {info.scopes.map((scope) => (
            <li key={scope.name} className="flex items-start gap-2">
              <Check
                aria-hidden
                className="mt-0.5 size-3.5 shrink-0 text-muted-foreground"
              />
              <span>
                {scope.description}{" "}
                <code className="rounded bg-muted px-1 font-mono text-[11px] text-muted-foreground">
                  {scope.name}
                </code>
              </span>
            </li>
          ))}
        </ul>
      </section>
      <section className="space-y-2">
        <h2 className="text-meta font-medium uppercase tracking-wider text-muted-foreground">
          On these projects only
        </h2>
        {info.projects.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            You have no projects yet, so there is nothing to give access to.
            Create one in the dashboard, then start the login again from the
            application.
          </p>
        ) : (
          <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border">
            {info.projects.map((project) => (
              <li key={project.id}>
                <label className="flex cursor-pointer items-center gap-2.5 px-3 py-2 text-sm hover:bg-accent/50">
                  <input
                    type="checkbox"
                    className="size-4 shrink-0 accent-action"
                    checked={ticked.has(project.id)}
                    onChange={() => toggle(project.id)}
                  />
                  <span className="min-w-0 break-words">
                    <span className="text-muted-foreground">
                      {project.org_name} /
                    </span>{" "}
                    <span className="font-medium">{project.name}</span>
                  </span>
                </label>
              </li>
            ))}
          </ul>
        )}
      </section>
      {approve.isError && !(approve.error instanceof UnauthenticatedError) && (
        <p
          role="alert"
          className="rounded-lg border border-deny/30 bg-deny/10 px-3 py-2 text-sm text-deny"
        >
          {approve.error.message}
        </p>
      )}
      <div className="grid grid-cols-2 gap-2">
        <Button
          variant="outline"
          disabled={approve.isPending || approve.isSuccess}
          onClick={() => leaveFor(denyRedirect(search))}
        >
          Deny
        </Button>
        <Button
          disabled={ticked.size === 0 || approve.isPending || approve.isSuccess}
          onClick={() => approve.mutate()}
        >
          Approve
        </Button>
      </div>
    </div>
  );
}

/** The name is whatever the client's own document says, so the hosts the
 * server derived from its URLs are shown beside it, as the facts. */
function ClientIdentity({ info }: { info: ConsentInfo }) {
  return (
    <div className="space-y-2 rounded-lg border border-border bg-muted/40 p-3 text-sm text-muted-foreground">
      <p>
        An application calling itself{" "}
        <strong className="text-foreground">
          {info.client_name ?? "(no name)"}
        </strong>{" "}
        wants to use Hexgate as you.
      </p>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        <dt>Published by</dt>
        <dd className="min-w-0 break-all font-mono font-semibold text-foreground">
          {info.client_host}
        </dd>
        <dt>Sends you back to</dt>
        <dd className="min-w-0 break-all font-mono font-semibold text-foreground">
          {info.redirect_host}
        </dd>
      </dl>
    </div>
  );
}
