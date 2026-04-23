import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { AppShell } from '@/components/AppShell'
import { DashboardPage } from '@/routes/Dashboard'
import { GraphPage } from '@/routes/Graph'
import { PlaygroundPage } from '@/routes/Playground'
import { AuditPage } from '@/routes/Audit'
import { TokensPage } from '@/routes/Tokens'
import { SettingsPage } from '@/routes/Settings'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<DashboardPage />} />
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
