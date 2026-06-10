import { useEffect, useState } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { ShieldCheck } from 'lucide-react'

import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  googleOAuthAvailable,
  startGoogleSignIn,
  useLogin,
  useUser,
} from '@/lib/auth'

const SignInSchema = z.object({
  email: z.string().email('Enter a valid email'),
  password: z.string().min(1, 'Required'),
})

type SignInValues = z.infer<typeof SignInSchema>

export function SignInPage() {
  const { user, loading } = useUser()
  const navigate = useNavigate()
  const location = useLocation()
  const login = useLogin()
  const [googleAvailable, setGoogleAvailable] = useState(false)

  // Probe once on mount — the button stays hidden when Google sign-in
  // isn't enabled on the backend (HEXGATE_GOOGLE_CLIENT_ID unset).
  useEffect(() => {
    googleOAuthAvailable().then(setGoogleAvailable)
  }, [])

  const form = useForm<SignInValues>({
    resolver: zodResolver(SignInSchema),
    defaultValues: { email: '', password: '' },
  })

  // If a session already exists, bounce to wherever the user was trying
  // to reach (preserved by ProtectedRoute) — or the dashboard root.
  if (!loading && user) {
    const target = (location.state as { from?: string } | null)?.from ?? '/'
    return <Navigate to={target} replace />
  }

  async function onSubmit(values: SignInValues) {
    try {
      await login.mutateAsync(values)
      const target = (location.state as { from?: string } | null)?.from ?? '/'
      navigate(target, { replace: true })
    } catch {
      // Error surfaces via login.error below — no toast needed.
    }
  }

  return (
    <AuthCardLayout>
      <Card className="w-full max-w-md">
        <CardHeader className="space-y-1">
          <div className="mb-2 flex justify-center">
            <ShieldCheck className="h-8 w-8 text-primary" />
          </div>
          <CardTitle className="text-center">Sign in to HexaGate</CardTitle>
          <CardDescription className="text-center">
            Authorization infrastructure for AI agents.
          </CardDescription>
        </CardHeader>

        <form onSubmit={form.handleSubmit(onSubmit)}>
          <CardContent className="space-y-4">
            {login.isError && (
              <Alert variant="destructive">
                <AlertDescription>
                  Incorrect email or password. Try again, or{' '}
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
                {...form.register('email')}
              />
              {form.formState.errors.email && (
                <p className="text-xs text-destructive">
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
                {...form.register('password')}
              />
              {form.formState.errors.password && (
                <p className="text-xs text-destructive">
                  {form.formState.errors.password.message}
                </p>
              )}
            </div>
          </CardContent>

          <CardFooter className="flex-col gap-3 pt-2">
            <Button
              type="submit"
              className="w-full"
              disabled={login.isPending || !form.formState.isValid}
            >
              {login.isPending ? 'Signing in…' : 'Sign in'}
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
              No account?{' '}
              <Link
                to="/sign-up"
                className="text-primary underline-offset-2 hover:underline"
              >
                Create one
              </Link>
            </p>
          </CardFooter>
        </form>
      </Card>
    </AuthCardLayout>
  )
}

/** Shared centred-card layout the auth pages all share. Extracted here
 * so the SignIn / SignUp / ForgotPassword / ResetPassword screens stay
 * visually coherent without copy-pasting the shell. */
export function AuthCardLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4 py-12">
      {children}
    </div>
  )
}
