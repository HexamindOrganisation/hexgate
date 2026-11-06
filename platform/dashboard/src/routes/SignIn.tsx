import { useEffect, useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";

import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  AuthLink,
  AuthShell,
  GoogleContinue,
} from "@/components/auth/AuthShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { googleOAuthAvailable, useLogin, useUser } from "@/lib/auth";
import { clearConsentReturn } from "@/lib/oauth_consent";

const SignInSchema = z.object({
  email: z.string().email("Enter a valid email"),
  password: z.string().min(1, "Required"),
});

type SignInValues = z.infer<typeof SignInSchema>;

export function SignInPage() {
  const { user, loading } = useUser();
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from;
  const login = useLogin();
  const [googleAvailable, setGoogleAvailable] = useState(false);

  // Probe once on mount — the button stays hidden when Google sign-in
  // isn't enabled on the backend (HEXGATE_GOOGLE_CLIENT_ID unset).
  useEffect(() => {
    googleOAuthAvailable().then(setGoogleAvailable);
  }, []);

  const form = useForm<SignInValues>({
    resolver: zodResolver(SignInSchema),
    defaultValues: { email: "", password: "" },
  });

  // If a session already exists, bounce to wherever the user was trying
  // to reach (preserved by ProtectedRoute) — or the dashboard root.
  if (!loading && user) {
    return <Navigate to={from ?? "/"} replace />;
  }

  async function onSubmit(values: SignInValues) {
    // A password sign-in returns through router state; a stash an abandoned
    // Google sign-in left must not take over.
    clearConsentReturn();
    try {
      await login.mutateAsync(values);
      navigate(from ?? "/", { replace: true });
    } catch {
      // Error surfaces via login.error below — no toast needed.
    }
  }

  return (
    <AuthShell
      title="Sign in to Hexgate"
      description="Authorization infrastructure for AI agents."
      footer={
        <>
          No account? <AuthLink to="/sign-up">Create one</AuthLink>
        </>
      }
    >
      <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
        {login.isError && (
          <Alert variant="destructive">
            <AlertDescription>
              Incorrect email or password. Try again, or{" "}
              <Link
                to="/forgot-password"
                className="underline underline-offset-2"
              >
                reset your password
              </Link>
              .
            </AlertDescription>
          </Alert>
        )}

        <div className="space-y-2">
          <Label htmlFor="email">Email</Label>
          <Input
            id="email"
            type="email"
            autoComplete="email"
            placeholder="you@example.com"
            {...form.register("email")}
          />
          {form.formState.errors.email && (
            <p className="text-xs text-deny">
              {form.formState.errors.email.message}
            </p>
          )}
        </div>

        <div className="space-y-2">
          <div className="flex items-baseline justify-between">
            <Label htmlFor="password">Password</Label>
            <Link
              to="/forgot-password"
              className="text-xs text-muted-foreground underline-offset-2 hover:underline"
            >
              Forgot?
            </Link>
          </div>
          <Input
            id="password"
            type="password"
            autoComplete="current-password"
            {...form.register("password")}
          />
          {form.formState.errors.password && (
            <p className="text-xs text-deny">
              {form.formState.errors.password.message}
            </p>
          )}
        </div>

        <Button
          type="submit"
          className="w-full"
          disabled={login.isPending || !form.formState.isValid}
        >
          {login.isPending ? "Signing in…" : "Sign in"}
        </Button>

        {googleAvailable && <GoogleContinue returnTo={from} />}
      </form>
    </AuthShell>
  );
}

/** The pre-redesign centred layout, kept for the OAuth consent page until it
 * moves onto AuthShell. */
export function AuthCardLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4 py-12">
      {children}
    </div>
  );
}
