import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { AppShell } from '@/components/AppShell'
import { ProtectedRoute } from '@/components/ProtectedRoute'
import { AgentsPage } from '@/routes/Agents'
import { AuditPage } from '@/routes/Audit'
import { DashboardPage } from '@/routes/Dashboard'
import { ForgotPasswordPage } from '@/routes/ForgotPassword'
import { GraphPage } from '@/routes/Graph'
import { PlaygroundPage } from '@/routes/Playground'
import { PoliciesPage } from '@/routes/Policies'
import { ResetPasswordPage } from '@/routes/ResetPassword'
import { SettingsPage } from '@/routes/Settings'
import { SignInPage } from '@/routes/SignIn'
import { SignUpPage } from '@/routes/SignUp'
import { TokensPage } from '@/routes/Tokens'
import { VerifyEmailPage } from '@/routes/VerifyEmail'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        {/* Public auth routes — no shell, no auth required. Token-bearing
            URLs (verify-email, reset-password) live here so the email
            links land directly without bouncing through ProtectedRoute. */}
        <Route path="/sign-in" element={<SignInPage />} />
        <Route path="/sign-up" element={<SignUpPage />} />
        <Route path="/forgot-password" element={<ForgotPasswordPage />} />
        <Route path="/reset-password/:token" element={<ResetPasswordPage />} />
        <Route path="/verify-email/:token" element={<VerifyEmailPage />} />

        {/* Authenticated dashboard. ProtectedRoute checks /v1/users/me
            and bounces signed-out visitors to /sign-in (preserving the
            requested path in state.from for post-sign-in redirect). */}
        <Route
          element={
            <ProtectedRoute>
              <AppShell />
            </ProtectedRoute>
          }
        >
          <Route index element={<DashboardPage />} />
          <Route path="agents" element={<AgentsPage />} />
          <Route path="policies" element={<PoliciesPage />} />
          <Route path="graph" element={<GraphPage />} />
          <Route path="playground" element={<PlaygroundPage />} />
          <Route path="audit" element={<AuditPage />} />
          <Route path="tokens" element={<TokensPage />} />
          <Route path="settings" element={<SettingsPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
