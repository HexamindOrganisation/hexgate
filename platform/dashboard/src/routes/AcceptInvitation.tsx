import { useLocation, useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";

import { AuthShell } from "@/components/auth/AuthShell";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
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
    return <AuthShell tone="muted" spin title="Loading invitation…" />;
  }

  if (preview.error) {
    return <PreviewError error={preview.error} />;
  }

  // ``preview.data`` is defined here — error and loading are both ruled
  // out above, and react-query's discriminated union narrows once both
  // are false.
  const inv = preview.data!;

  if (!user) {
    return <SignedOut invitation={inv} inviteId={inviteId} />;
  }

  // Case-insensitive — the backend matches lowercased emails on
  // accept, so the frontend's "mismatch" card must use the same rule
  // or we'd flash a scary warning at users whose email casing the
  // mailer normalised (e.g. Alice@... vs alice@...).
  const emailMatches = user.email.toLowerCase() === inv.email.toLowerCase();

  if (!emailMatches) {
    return <EmailMismatch invitation={inv} signedInAs={user.email} />;
  }

  return <AcceptCard invitation={inv} inviteId={inviteId} />;
}

// ---------------------------------------------------------------------------
// One per state-machine branch
// ---------------------------------------------------------------------------

function PreviewError({ error }: { error: ApiError }) {
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
    <AuthShell tone="error" title={title} description={body} card={false}>
      <Button variant="outline" asChild className="w-full">
        <a href="/">Go to dashboard</a>
      </Button>
    </AuthShell>
  );
}

function SignedOut({
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
    <AuthShell
      title={`You're invited to ${invitation.org_name}`}
      description={
        <>
          <span className="font-medium text-foreground">
            {invitation.invited_by_email}
          </span>{" "}
          invited{" "}
          <span className="font-medium text-foreground">
            {invitation.email}
          </span>{" "}
          to join as <span className="capitalize">{invitation.role}</span>.
        </>
      }
    >
      <div className="space-y-4">
        <Alert>
          <AlertDescription>
            Sign in as <span className="font-medium">{invitation.email}</span>{" "}
            to accept this invitation.
          </AlertDescription>
        </Alert>
        <div className="space-y-2">
          <Button className="w-full" onClick={goSignIn}>
            Sign in to accept
          </Button>
          <Button variant="outline" className="w-full" onClick={goSignUp}>
            Create an account
          </Button>
        </div>
        {/* Preserve the inviteId in the markup so e2e tests can read it
            for debugging; harmless otherwise. */}
        <span className="sr-only">Invitation {inviteId}</span>
      </div>
    </AuthShell>
  );
}

function EmailMismatch({
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
    <AuthShell
      tone="error"
      title="This invite isn't for this account"
      description={
        <>
          The invitation is for{" "}
          <span className="font-medium text-foreground">
            {invitation.email}
          </span>
          , but you're signed in as{" "}
          <span className="font-medium text-foreground">{signedInAs}</span>.
        </>
      }
    >
      <div className="space-y-4">
        <Alert variant="destructive">
          <AlertDescription>
            Sign out and sign back in with{" "}
            <span className="font-medium">{invitation.email}</span> to accept,
            or ask the inviter to re-send to your current address.
          </AlertDescription>
        </Alert>
        <Button
          className="w-full"
          onClick={switchAccount}
          disabled={logout.isPending}
        >
          {logout.isPending ? "Signing out…" : "Sign out & switch account"}
        </Button>
      </div>
    </AuthShell>
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
    <AuthShell
      title={`Join ${invitation.org_name}?`}
      description={
        <>
          <span className="font-medium text-foreground">
            {invitation.invited_by_email}
          </span>{" "}
          invited you to join as{" "}
          <span className="capitalize text-foreground">{invitation.role}</span>.
        </>
      }
    >
      <div className="space-y-4">
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
        <div className="space-y-2">
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
        </div>
      </div>
    </AuthShell>
  );
}
