import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { MailCheck } from "lucide-react";

import { AuthLink, AuthShell } from "@/components/auth/AuthShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useForgotPassword } from "@/lib/auth";

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
      <AuthShell
        tone="success"
        icon={MailCheck}
        title="Check your email"
        description={
          <>
            If an account exists for {form.getValues("email")}, we just sent a
            reset link to it. Open it and follow the prompt within an hour.
          </>
        }
        footer={<AuthLink to="/sign-in">Back to sign in</AuthLink>}
      />
    );
  }

  return (
    <AuthShell
      title="Reset your password"
      description="Enter the email on your account and we'll send a reset link."
      footer={<AuthLink to="/sign-in">Back to sign in</AuthLink>}
    >
      <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
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
        <Button type="submit" className="w-full" disabled={forgot.isPending}>
          {forgot.isPending ? "Sending…" : "Send reset link"}
        </Button>
      </form>
    </AuthShell>
  );
}
