import { useLocation, useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";
import { Loader2, ShieldCheck, XCircle } from "lucide-react";

import { AuthCardLayout } from "@/routes/SignIn";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ApiError } from "@/lib/api";
import { useActive } from "@/lib/active";
import { useLogout, useUser } from "@/lib/auth";
import {
  type InvitationPreview,
  useAcceptInvitation,
  useInvitationPreview,
} from "@/lib/invites";

/**
 * Public landing page for emailed invitation links.
 *
 * URL: ``/invites/:inviteId/accept`` (mounted OUTSIDE ProtectedRoute so
 * the magic-link email lands here without bouncing through /sign-in
 * first — the page itself handles the "you need to sign in" branch and
 * preserves the invite URL in router state so the post-sign-in
 * redirect lands the user right back here).
 *
 * The page is a state machine over (preview-query state) × (user
 * signed-in?, email-matches?). Each branch renders its own card; we
 * never show two cards or a half-rendered Accept button.
 */
export function AcceptInvitationPage() {
  const { inviteId } = useParams<{ inviteId: string }>();
  const preview = useInvitationPreview(inviteId ?? "");
  const { user, loading: userLoading } = useUser();

  // Loading both queries before deciding which card to show — otherwise
  // we'd flash "sign in to accept" for a moment while /users/me is
  // still in flight on a signed-in tab.
  if (!inviteId || preview.isLoading || userLoading) {
    return (
      <AuthCardLayout>
        <LoadingCard />
      </AuthCardLayout>
    );
  }

  if (preview.error) {
    return (
      <AuthCardLayout>
        <PreviewErrorCard error={preview.error} />
      </AuthCardLayout>
    );
  }

  // ``preview.data`` is defined here — error and loading are both ruled
  // out above, and react-query's discriminated union narrows once both
  // are false.
  const inv = preview.data!;

  if (!user) {
    return (
      <AuthCardLayout>
        <SignedOutCard invitation={inv} inviteId={inviteId} />
      </AuthCardLayout>
    );
  }

  // Case-insensitive — the backend matches lowercased emails on
  // accept, so the frontend's "mismatch" card must use the same rule
  // or we'd flash a scary warning at users whose email casing the
  // mailer normalised (e.g. Alice@... vs alice@...).
  const emailMatches = user.email.toLowerCase() === inv.email.toLowerCase();

  if (!emailMatches) {
    return (
      <AuthCardLayout>
        <EmailMismatchCard invitation={inv} signedInAs={user.email} />
      </AuthCardLayout>
    );
  }

  return (
    <AuthCardLayout>
      <AcceptCard invitation={inv} inviteId={inviteId} />
    </AuthCardLayout>
  );
}

// ---------------------------------------------------------------------------
// Card variants — one per state-machine branch
// ---------------------------------------------------------------------------

function LoadingCard() {
  return (
    <Card className="w-full max-w-md">
      <CardContent className="flex items-center gap-3 py-10 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading invitation…
      </CardContent>
    </Card>
  );
}

function PreviewErrorCard({ error }: { error: ApiError }) {
  // 404 → "this link is broken or the invite was withdrawn"
  // 410 → "expired or already used"
  // Other → generic "couldn't load" — covers transient network blips
  // and the unlikely 5xx case. Backend doesn't return 401/403 here
  // because preview is public.
  let title = "Invitation unavailable";
  let body =
    "We couldn't load this invitation. Please try the link again later.";
  if (error.status === 404) {
    title = "Invitation not found";
    body =
      "This link is broken, or the invitation was cancelled. Ask the person who invited you to send a fresh one.";
  } else if (error.status === 410) {
    title = "Invitation expired";
    body =
      "This invitation has expired or has already been used. Ask an admin for a new one if you still need access.";
  }

  return (
    <Card className="w-full max-w-md">
      <CardHeader className="space-y-1">
        <div className="mb-2 flex justify-center">
          <XCircle className="h-8 w-8 text-destructive" />
        </div>
        <CardTitle className="text-center">{title}</CardTitle>
        <CardDescription className="text-center">{body}</CardDescription>
      </CardHeader>
      <CardFooter className="justify-center">
        <Button variant="outline" asChild>
          <a href="/">Go to dashboard</a>
        </Button>
      </CardFooter>
    </Card>
  );
}

function SignedOutCard({
  invitation,
  inviteId,
}: {
  invitation: InvitationPreview;
  inviteId: string;
}) {
  const navigate = useNavigate();
  const location = useLocation();

  // Preserve the invite URL as ``state.from`` so SignIn bounces the
  // user straight back here after a successful login. Mirrors the
  // ProtectedRoute pattern used by every other authed page.
  function goSignIn() {
    const from = location.pathname + location.search;
    navigate("/sign-in", { state: { from } });
  }

  function goSignUp() {
    const from = location.pathname + location.search;
    navigate("/sign-up", { state: { from } });
  }

  return (
    <Card className="w-full max-w-md">
      <CardHeader className="space-y-1">
        <div className="mb-2 flex justify-center">
          <ShieldCheck className="h-8 w-8 text-primary" />
        </div>
        <CardTitle className="text-center">
          You're invited to {invitation.org_name}
        </CardTitle>
        <CardDescription className="text-center">
          <span className="font-medium text-foreground">
            {invitation.invited_by_email}
          </span>{" "}
          invited{" "}
          <span className="font-medium text-foreground">
            {invitation.email}
          </span>{" "}
          to join as <span className="capitalize">{invitation.role}</span>.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Alert>
          <AlertDescription>
            Sign in as <span className="font-medium">{invitation.email}</span>{" "}
            to accept this invitation.
          </AlertDescription>
        </Alert>
      </CardContent>
      <CardFooter className="flex-col gap-2">
        <Button className="w-full" onClick={goSignIn}>
          Sign in to accept
        </Button>
        <Button variant="ghost" className="w-full" onClick={goSignUp}>
          Create an account
        </Button>
        {/* Preserve the inviteId in the markup so e2e tests can read it
            for debugging; harmless otherwise. */}
        <span className="sr-only">Invitation {inviteId}</span>
      </CardFooter>
    </Card>
  );
}

function EmailMismatchCard({
  invitation,
  signedInAs,
}: {
  invitation: InvitationPreview;
  signedInAs: string;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const logout = useLogout();

  async function switchAccount() {
    try {
      await logout.mutateAsync();
    } catch {
      // Logging out while already-logged-out is fine — we'll send them
      // to sign-in regardless. ``useLogout`` already swallows 401.
    }
    const from = location.pathname + location.search;
    navigate("/sign-in", { state: { from }, replace: true });
  }

  return (
    <Card className="w-full max-w-md">
      <CardHeader className="space-y-1">
        <div className="mb-2 flex justify-center">
          <XCircle className="h-8 w-8 text-destructive" />
        </div>
        <CardTitle className="text-center">
          This invite isn't for this account
        </CardTitle>
        <CardDescription className="text-center">
          The invitation is for{" "}
          <span className="font-medium text-foreground">
            {invitation.email}
          </span>
          , but you're signed in as{" "}
          <span className="font-medium text-foreground">{signedInAs}</span>.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Alert variant="destructive">
          <AlertDescription>
            Sign out and sign back in with{" "}
            <span className="font-medium">{invitation.email}</span> to accept,
            or ask the inviter to re-send to your current address.
          </AlertDescription>
        </Alert>
      </CardContent>
      <CardFooter>
        <Button
          className="w-full"
          onClick={switchAccount}
          disabled={logout.isPending}
        >
          {logout.isPending ? "Signing out…" : "Sign out & switch account"}
        </Button>
      </CardFooter>
    </Card>
  );
}

function AcceptCard({
  invitation,
  inviteId,
}: {
  invitation: InvitationPreview;
  inviteId: string;
}) {
  const navigate = useNavigate();
  const accept = useAcceptInvitation();
  const setActiveOrg = useActive((s) => s.setActiveOrg);

  // Translate the backend's tagged error responses into friendly
  // copy. The race-case 410/409 happens if the invite expired or got
  // revoked between the preview fetch and the accept click; the 403
  // shouldn't happen here (we already gated on email match) but is
  // surfaced defensively in case the user's email changed under us.
  function translateError(err: unknown): string {
    if (err instanceof ApiError) {
      if (err.status === 410) return "This invitation has expired.";
      if (err.status === 409) return "This invitation has already been used.";
      if (err.status === 403) return "This invitation isn't for this account.";
    }
    return "Couldn't accept the invitation. Please try again.";
  }

  async function onAccept() {
    try {
      await accept.mutateAsync(inviteId);
      toast.success(`Joined ${invitation.org_name}`);
      // Drop the user into the new org. ``setActiveOrg`` clears the
      // active project as a side-effect — the project picker will
      // populate from the org's project list on the next page.
      setActiveOrg(invitation.org_id);
      // Members page is the most useful landing — the user can
      // immediately see who else is in the org they just joined.
      navigate(`/orgs/${invitation.org_id}/members`, { replace: true });
    } catch (err) {
      toast.error(translateError(err));
    }
  }

  const errorText = accept.error ? translateError(accept.error) : null;

  return (
    <Card className="w-full max-w-md">
      <CardHeader className="space-y-1">
        <div className="mb-2 flex justify-center">
          <ShieldCheck className="h-8 w-8 text-primary" />
        </div>
        <CardTitle className="text-center">
          Join {invitation.org_name}?
        </CardTitle>
        <CardDescription className="text-center">
          <span className="font-medium text-foreground">
            {invitation.invited_by_email}
          </span>{" "}
          invited you to join as{" "}
          <span className="capitalize text-foreground">{invitation.role}</span>.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {errorText && (
          <Alert variant="destructive">
            <AlertDescription>{errorText}</AlertDescription>
          </Alert>
        )}
        <dl className="grid grid-cols-3 gap-2 text-sm">
          <dt className="text-muted-foreground">Organization</dt>
          <dd className="col-span-2 font-medium">
            {invitation.org_name}{" "}
            <span className="font-mono text-xs text-muted-foreground">
              ({invitation.org_slug})
            </span>
          </dd>
          <dt className="text-muted-foreground">Your role</dt>
          <dd className="col-span-2 capitalize">{invitation.role}</dd>
          <dt className="text-muted-foreground">Email</dt>
          <dd className="col-span-2 font-mono text-xs">{invitation.email}</dd>
        </dl>
      </CardContent>
      <CardFooter className="flex-col gap-2">
        <Button
          className="w-full"
          onClick={onAccept}
          disabled={accept.isPending}
        >
          {accept.isPending ? "Joining…" : "Accept invitation"}
        </Button>
        <Button
          variant="ghost"
          className="w-full"
          onClick={() => navigate("/", { replace: true })}
          disabled={accept.isPending}
        >
          Not now
        </Button>
      </CardFooter>
    </Card>
  );
}
