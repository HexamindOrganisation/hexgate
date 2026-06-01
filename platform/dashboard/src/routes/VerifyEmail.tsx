import { useEffect, useRef } from 'react'
import { Link, useParams } from 'react-router-dom'
import { CheckCircle2, MailX } from 'lucide-react'

import { Button } from '@/components/ui/button'
import {
  Card,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { useUser, useVerifyEmail } from '@/lib/auth'
import { AuthCardLayout } from './SignIn'

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
 */
export function VerifyEmailPage() {
  const { token } = useParams<{ token: string }>()
  const verify = useVerifyEmail()
  const { user } = useUser()
  const firedRef = useRef(false)

  useEffect(() => {
    // React StrictMode in dev mounts each component twice; the token
    // can only be consumed once, so guard against the double-fire.
    if (token && !firedRef.current) {
      firedRef.current = true
      verify.mutate(token)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token])

  if (!token) {
    return (
      <AuthCardLayout>
        <Card className="w-full max-w-md">
          <CardHeader>
            <CardTitle className="text-center">Invalid verification link</CardTitle>
            <CardDescription className="text-center">
              This link is missing its token. Sign in and request a fresh
              verification email from your account.
            </CardDescription>
          </CardHeader>
          <CardFooter>
            <Button asChild variant="ghost" className="w-full">
              <Link to="/sign-in">Sign in</Link>
            </Button>
          </CardFooter>
        </Card>
      </AuthCardLayout>
    )
  }

  if (verify.isPending || verify.isIdle) {
    return (
      <AuthCardLayout>
        <Card className="w-full max-w-md">
          <CardHeader>
            <CardTitle className="text-center">Verifying…</CardTitle>
            <CardDescription className="text-center">
              Hang tight — one moment.
            </CardDescription>
          </CardHeader>
        </Card>
      </AuthCardLayout>
    )
  }

  if (verify.isSuccess) {
    return (
      <AuthCardLayout>
        <Card className="w-full max-w-md">
          <CardHeader>
            <div className="mb-2 flex justify-center">
              <CheckCircle2 className="h-8 w-8 text-emerald-400" />
            </div>
            <CardTitle className="text-center">Email verified</CardTitle>
            <CardDescription className="text-center">
              You're all set.
            </CardDescription>
          </CardHeader>
          <CardFooter>
            <Button asChild className="w-full">
              <Link to={user ? '/' : '/sign-in'}>
                {user ? 'Continue to dashboard' : 'Sign in'}
              </Link>
            </Button>
          </CardFooter>
        </Card>
      </AuthCardLayout>
    )
  }

  return (
    <AuthCardLayout>
      <Card className="w-full max-w-md">
        <CardHeader>
          <div className="mb-2 flex justify-center">
            <MailX className="h-8 w-8 text-destructive" />
          </div>
          <CardTitle className="text-center">Verification failed</CardTitle>
          <CardDescription className="text-center">
            This verification link is expired or invalid. Sign in and request
            a fresh one from the banner at the top of the dashboard.
          </CardDescription>
        </CardHeader>
        <CardFooter>
          <Button asChild variant="ghost" className="w-full">
            <Link to="/sign-in">Back to sign in</Link>
          </Button>
        </CardFooter>
      </Card>
    </AuthCardLayout>
  )
}
