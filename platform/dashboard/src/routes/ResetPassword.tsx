import { Link, useNavigate, useParams } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";

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
import { useResetPassword } from "@/lib/auth";
import { AuthCardLayout } from "./SignIn";

const Schema = z
  .object({
    password: z.string().min(8, "At least 8 characters"),
    confirm: z.string(),
  })
  .refine((v) => v.password === v.confirm, {
    message: "Passwords don't match",
    path: ["confirm"],
  });

type Values = z.infer<typeof Schema>;

export function ResetPasswordPage() {
  const { token } = useParams<{ token: string }>();
  const navigate = useNavigate();
  const reset = useResetPassword();
  const form = useForm<Values>({
    resolver: zodResolver(Schema),
    defaultValues: { password: "", confirm: "" },
  });

  // Token comes from the URL — emailed links land here directly. The
  // backend validates the JWT signature + expiry on /reset-password,
  // so we don't pre-check here; a tampered or expired token surfaces
  // as the .isError branch below.
  async function onSubmit(values: Values) {
    if (!token) return;
    try {
      await reset.mutateAsync({ token, password: values.password });
      // After 2 seconds on the success card, bounce to sign-in.
      setTimeout(() => navigate("/sign-in", { replace: true }), 2000);
    } catch {
      // surfaces via reset.isError
    }
  }

  if (!token) {
    return (
      <AuthCardLayout>
        <Card className="w-full max-w-md">
          <CardHeader>
            <CardTitle className="text-center">Invalid reset link</CardTitle>
            <CardDescription className="text-center">
              This reset link is missing a token. Request a new one from the
              forgot-password page.
            </CardDescription>
          </CardHeader>
          <CardFooter>
            <Button asChild variant="ghost" className="w-full">
              <Link to="/forgot-password">Request a new link</Link>
            </Button>
          </CardFooter>
        </Card>
      </AuthCardLayout>
    );
  }

  if (reset.isSuccess) {
    return (
      <AuthCardLayout>
        <Card className="w-full max-w-md">
          <CardHeader>
            <CardTitle className="text-center">Password updated</CardTitle>
            <CardDescription className="text-center">
              Sending you to sign in…
            </CardDescription>
          </CardHeader>
        </Card>
      </AuthCardLayout>
    );
  }

  return (
    <AuthCardLayout>
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="text-center">Set a new password</CardTitle>
          <CardDescription className="text-center">
            Pick something you don't use anywhere else.
          </CardDescription>
        </CardHeader>
        <form onSubmit={form.handleSubmit(onSubmit)}>
          <CardContent className="space-y-4">
            {reset.isError && (
              <Alert variant="destructive">
                <AlertDescription>
                  This reset link has expired or is invalid. Request a fresh one
                  from the{" "}
                  <Link
                    to="/forgot-password"
                    className="underline underline-offset-2"
                  >
                    forgot-password page
                  </Link>
                  .
                </AlertDescription>
              </Alert>
            )}

            <div className="space-y-2">
              <Label htmlFor="password">New password</Label>
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
              <Label htmlFor="confirm">Confirm new password</Label>
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
          <CardFooter>
            <Button type="submit" className="w-full" disabled={reset.isPending}>
              {reset.isPending ? "Updating…" : "Update password"}
            </Button>
          </CardFooter>
        </form>
      </Card>
    </AuthCardLayout>
  );
}
