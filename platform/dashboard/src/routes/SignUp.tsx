import { useEffect, useState } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { ShieldCheck } from "lucide-react";

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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  googleOAuthAvailable,
  startGoogleSignIn,
  useLogin,
  useRegister,
  useRequestVerify,
  useUser,
} from "@/lib/auth";
import { AuthCardLayout } from "./SignIn";

const SignUpSchema = z
  .object({
    email: z.string().email("Enter a valid email"),
    // FastAPI Users' default password rule is min length 8 — match here
    // so the client-side preview matches the server check exactly. When
    // we tighten server rules in Phase 3d-tail, update both in lock-step.
    password: z.string().min(8, "At least 8 characters"),
    confirm: z.string(),
  })
  .refine((v) => v.password === v.confirm, {
    message: "Passwords don't match",
    path: ["confirm"],
  });

type SignUpValues = z.infer<typeof SignUpSchema>;

export function SignUpPage() {
  const { user, loading } = useUser();
  const navigate = useNavigate();
  const register = useRegister();
  const login = useLogin();
  const requestVerify = useRequestVerify();
  const [googleAvailable, setGoogleAvailable] = useState(false);

  useEffect(() => {
    googleOAuthAvailable().then(setGoogleAvailable);
  }, []);

  const form = useForm<SignUpValues>({
    resolver: zodResolver(SignUpSchema),
    defaultValues: { email: "", password: "", confirm: "" },
  });

  if (!loading && user) return <Navigate to="/" replace />;

  async function onSubmit(values: SignUpValues) {
    try {
      // Three-step: create → log in → request the verification email
      // so the new user lands on the dashboard already signed in with
      // a verification token in their inbox.
      await register.mutateAsync({
        email: values.email,
        password: values.password,
      });
      await login.mutateAsync({
        email: values.email,
        password: values.password,
      });
      // Kick off verification — non-blocking; user already signed in.
      requestVerify.mutate(values.email);
      navigate("/", { replace: true });
    } catch {
      // Error path renders via register.error / login.error below.
    }
  }

  const registerError = register.error;
  const duplicateEmail =
    registerError && extractDetail(registerError).includes("exists");

  return (
    <AuthCardLayout>
      <Card className="w-full max-w-md">
        <CardHeader className="space-y-1">
          <div className="mb-2 flex justify-center">
            <ShieldCheck className="h-8 w-8 text-primary" />
          </div>
          <CardTitle className="text-center">Create your account</CardTitle>
          <CardDescription className="text-center">
            Email and password. We'll send a verification link.
          </CardDescription>
        </CardHeader>

        <form onSubmit={form.handleSubmit(onSubmit)}>
          <CardContent className="space-y-4">
            {register.isError && (
              <Alert variant="destructive">
                <AlertDescription>
                  {duplicateEmail
                    ? "An account with that email already exists. Try signing in instead."
                    : "Could not create the account. Please try again."}
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
                <p className="text-xs text-destructive">
                  {form.formState.errors.email.message}
                </p>
              )}
            </div>

            <div className="space-y-2">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                autoComplete="new-password"
                {...form.register("password")}
              />
              {form.formState.errors.password && (
                <p className="text-xs text-destructive">
                  {form.formState.errors.password.message}
                </p>
              )}
            </div>

            <div className="space-y-2">
              <Label htmlFor="confirm">Confirm password</Label>
              <Input
                id="confirm"
                type="password"
                autoComplete="new-password"
                {...form.register("confirm")}
              />
              {form.formState.errors.confirm && (
                <p className="text-xs text-destructive">
                  {form.formState.errors.confirm.message}
                </p>
              )}
            </div>
          </CardContent>

          <CardFooter className="flex-col gap-3 pt-2">
            <Button
              type="submit"
              className="w-full"
              disabled={register.isPending || login.isPending}
            >
              {register.isPending || login.isPending
                ? "Creating…"
                : "Create account"}
            </Button>

            {googleAvailable && (
              <>
                <div className="relative w-full">
                  <div className="absolute inset-0 flex items-center">
                    <span className="w-full border-t border-border" />
                  </div>
                  <div className="relative flex justify-center text-xs uppercase">
                    <span className="bg-card px-2 text-muted-foreground">
                      or
                    </span>
                  </div>
                </div>

                <Button
                  type="button"
                  variant="outline"
                  className="w-full"
                  onClick={() => startGoogleSignIn().catch(() => undefined)}
                >
                  Continue with Google
                </Button>
              </>
            )}

            <p className="text-center text-sm text-muted-foreground">
              Already have an account?{" "}
              <Link
                to="/sign-in"
                className="text-primary underline-offset-2 hover:underline"
              >
                Sign in
              </Link>
            </p>
          </CardFooter>
        </form>
      </Card>
    </AuthCardLayout>
  );
}

function extractDetail(err: unknown): string {
  if (err && typeof err === "object" && "detail" in err) {
    const d = (err as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (typeof d === "object" && d !== null && "detail" in d) {
      const inner = (d as { detail: unknown }).detail;
      if (typeof inner === "string") return inner;
    }
  }
  return "";
}
