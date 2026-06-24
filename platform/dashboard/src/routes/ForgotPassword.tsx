import { Link } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { MailCheck } from "lucide-react";

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
import { useForgotPassword } from "@/lib/auth";
import { AuthCardLayout } from "./SignIn";

const Schema = z.object({ email: z.string().email("Enter a valid email") });
type Values = z.infer<typeof Schema>;

export function ForgotPasswordPage() {
  const forgot = useForgotPassword();
  const form = useForm<Values>({
    resolver: zodResolver(Schema),
    defaultValues: { email: "" },
  });

  async function onSubmit(values: Values) {
    // The backend always returns 202 — same status whether the email
    // exists or not, so we don't leak which addresses are registered.
    // The UI mirrors that opacity: success message regardless.
    await forgot.mutateAsync(values.email).catch(() => undefined);
  }

  if (forgot.isSuccess) {
    return (
      <AuthCardLayout>
        <Card className="w-full max-w-md">
          <CardHeader>
            <div className="mb-2 flex justify-center">
              <MailCheck className="h-8 w-8 text-primary" />
            </div>
            <CardTitle className="text-center">Check your email</CardTitle>
            <CardDescription className="text-center">
              If an account exists for {form.getValues("email")}, we just sent a
              reset link to it. Open it and follow the prompt within an hour.
            </CardDescription>
          </CardHeader>
          <CardFooter>
            <Button asChild variant="ghost" className="w-full">
              <Link to="/sign-in">Back to sign in</Link>
            </Button>
          </CardFooter>
        </Card>
      </AuthCardLayout>
    );
  }

  return (
    <AuthCardLayout>
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="text-center">Reset your password</CardTitle>
          <CardDescription className="text-center">
            Enter the email on your account and we'll send a reset link.
          </CardDescription>
        </CardHeader>
        <form onSubmit={form.handleSubmit(onSubmit)}>
          <CardContent className="space-y-4">
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
          </CardContent>
          <CardFooter className="flex-col gap-3">
            <Button
              type="submit"
              className="w-full"
              disabled={forgot.isPending}
            >
              {forgot.isPending ? "Sending…" : "Send reset link"}
            </Button>
            <p className="text-center text-sm text-muted-foreground">
              <Link
                to="/sign-in"
                className="text-primary underline-offset-2 hover:underline"
              >
                Back to sign in
              </Link>
            </p>
          </CardFooter>
        </form>
      </Card>
    </AuthCardLayout>
  );
}
