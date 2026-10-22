import { Link, useParams } from "react-router-dom";
import { MailX } from "lucide-react";

import { AuthLink, AuthShell } from "@/components/auth/AuthShell";
import { Button } from "@/components/ui/button";
import { useUser, useVerifyEmail } from "@/lib/auth";

/**
 * Landing page for the verify-email link. Auto-consumes the token on
 * mount — the user just clicks "Verify" in their inbox and gets a
 * success card without any extra click.
 *
 * Three terminal states:
 *   - success → "you're verified, head to dashboard" (signed in → /,
 *     signed out → /sign-in)
 *   - failure → expired/invalid token, link to resend
 *   - missing token → invalid URL
 *
 * The verify call is modelled as a one-shot ``useQuery`` keyed by the
 * token (see ``useVerifyEmail`` in lib/auth.ts). Previous useMutation +
 * useEffect + useRef pattern hung at "Verifying…" forever under React
 * 18+ StrictMode dev because the mutation observer got torn down
 * during the simulated unmount/remount and the remount's fresh observer
 * never reconnected to the in-flight request.
 */
export function VerifyEmailPage() {
  const { token } = useParams<{ token: string }>();
  const verify = useVerifyEmail(token);
  const { user } = useUser();

  if (!token) {
    return (
      <AuthShell
        tone="error"
        title="Invalid verification link"
        description="This link is missing its token. Sign in and request a fresh verification email from your account."
        footer={<AuthLink to="/sign-in">Sign in</AuthLink>}
      />
    );
  }

  // Query is in flight on first mount + while the request runs.
  // ``isLoading`` is the "no data yet, not errored" state.
  if (verify.isLoading) {
    return (
      <AuthShell
        tone="muted"
        spin
        title="Verifying…"
        description="Hang tight — one moment."
      />
    );
  }

  if (verify.isSuccess) {
    return (
      <AuthShell
        tone="success"
        title="Email verified"
        description="You're all set."
        card={false}
      >
        <Button asChild className="w-full">
          <Link to={user ? "/" : "/sign-in"}>
            {user ? "Continue to dashboard" : "Sign in"}
          </Link>
        </Button>
      </AuthShell>
    );
  }

  return (
    <AuthShell
      tone="error"
      icon={MailX}
      title="Verification failed"
      description="This verification link is expired or invalid. Sign in and request a fresh one from the banner at the top of the dashboard."
      footer={<AuthLink to="/sign-in">Back to sign in</AuthLink>}
    />
  );
}
