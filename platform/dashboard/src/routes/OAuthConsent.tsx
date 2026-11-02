import { useEffect, useState } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";
import { useLocation } from "react-router-dom";
import { ShieldCheck } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { oauthApi, UnauthenticatedError, type ConsentInfo } from "@/lib/api";
import { USER_QUERY_KEY } from "@/lib/auth";
import {
  denyRedirect,
  hasRepeatedParams,
  isLeavableUrl,
  leaveFor,
  redirectUri,
} from "@/lib/oauth_consent";
import { AuthCardLayout } from "@/routes/SignIn";

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

  return (
    <AuthCardLayout>
      <Card className="w-full max-w-lg">
        <CardHeader className="space-y-1">
          <div className="mb-2 flex justify-center">
            <ShieldCheck className="h-8 w-8 text-primary" />
          </div>
          <CardTitle className="text-center">
            Authorize access to Hexgate
          </CardTitle>
        </CardHeader>
        {info.isPending && (
          <CardContent className="text-sm text-muted-foreground">
            Loading…
          </CardContent>
        )}
        {problem && (
          <CardContent>
            <Alert variant="destructive">
              <AlertDescription>{problem}</AlertDescription>
            </Alert>
          </CardContent>
        )}
        {info.isSuccess && !problem && (
          <ConsentForm info={info.data} search={search} />
        )}
      </Card>
    </AuthCardLayout>
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
    <>
      <CardContent className="space-y-5">
        <ClientIdentity info={info} />
        <section className="space-y-2">
          <h2 className="text-sm font-medium">It asks to</h2>
          <ul className="space-y-1 text-sm">
            {info.scopes.map((scope) => (
              <li key={scope.name}>
                {scope.description}{" "}
                <code className="text-xs text-muted-foreground">
                  {scope.name}
                </code>
              </li>
            ))}
          </ul>
        </section>
        <section className="space-y-2">
          <h2 className="text-sm font-medium">On these projects only</h2>
          {info.projects.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              You have no projects yet, so there is nothing to give access to.
              Create one in the dashboard, then start the login again from the
              application.
            </p>
          ) : (
            <ul className="space-y-1 text-sm">
              {info.projects.map((project) => (
                <li key={project.id}>
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={ticked.has(project.id)}
                      onChange={() => toggle(project.id)}
                    />
                    <span className="text-muted-foreground">
                      {project.org_name} /
                    </span>
                    {project.name}
                  </label>
                </li>
              ))}
            </ul>
          )}
        </section>
        {approve.isError &&
          !(approve.error instanceof UnauthenticatedError) && (
            <Alert variant="destructive">
              <AlertDescription>{approve.error.message}</AlertDescription>
            </Alert>
          )}
      </CardContent>
      <CardFooter className="justify-end gap-2">
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
      </CardFooter>
    </>
  );
}

/** The name is whatever the client's own document says, so the hosts the
 * server derived from its URLs are shown beside it, as the facts. */
function ClientIdentity({ info }: { info: ConsentInfo }) {
  return (
    <div className="space-y-2 text-sm text-muted-foreground">
      <p>
        An application calling itself{" "}
        <strong className="text-foreground">
          {info.client_name ?? "(no name)"}
        </strong>{" "}
        wants to use Hexgate as you.
      </p>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        <dt>Published by</dt>
        <dd className="font-mono font-semibold text-foreground">
          {info.client_host}
        </dd>
        <dt>Sends you back to</dt>
        <dd className="font-mono font-semibold text-foreground">
          {info.redirect_host}
        </dd>
      </dl>
    </div>
  );
}
