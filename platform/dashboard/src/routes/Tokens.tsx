import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  BookOpen,
  Check,
  Copy,
  Eye,
  EyeOff,
  Fingerprint,
  Filter,
  KeyRound,
  Plus,
  RefreshCcw,
  Trash2,
  Clock,
  AlertTriangle,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { NoProjectEmptyState } from '@/components/NoProjectEmptyState'
import { api, type TokenMintResponse } from '@/lib/api'
import { useProjectScoped } from '@/lib/active'
import { cn } from '@/lib/utils'

function formatRelative(iso: string | null): string {
  if (!iso) return 'Never'
  const then = new Date(iso).getTime()
  const diff = Date.now() - then
  if (diff < 60_000) return 'just now'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`
  return `${Math.floor(diff / 86_400_000)}d ago`
}

function formatCreated(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
  })
}

function CopyButton({ value, size = 'sm' }: { value: string; size?: 'sm' | 'default' }) {
  const [copied, setCopied] = useState(false)
  return (
    <Button
      variant="ghost"
      size={size === 'sm' ? 'icon' : 'default'}
      onClick={async () => {
        await navigator.clipboard.writeText(value)
        setCopied(true)
        setTimeout(() => setCopied(false), 1200)
      }}
      className={size === 'sm' ? 'size-7' : undefined}
    >
      {copied ? <Check className="text-allow" /> : <Copy />}
    </Button>
  )
}

function JustMintedBanner({
  token,
  onDismiss,
}: {
  token: TokenMintResponse
  onDismiss: () => void
}) {
  // Masked by default \u2014 operators copy-paste regardless of what's
  // rendered, so there's no UX cost, and a shoulder-surfing screenshot
  // (or auto-screenshare during a demo) doesn't leak the secret. The
  // mask keeps just the env-tagged prefix and the last 4 chars so the
  // operator can still visually distinguish multiple tokens; `Reveal`
  // brings the rest back.
  const [revealed, setRevealed] = useState(false)
  // Tokens are `fty_(test|live)_<uuid>_<biscuit>` \u2014 keep the 9-char
  // env-tagged prefix and the last 4 of the biscuit; everything in
  // between is the project UUID + opaque biscuit bytes (nothing useful
  // to expose at rest).
  const prefixEnd = token.full.indexOf('_', 4) + 1
  const masked =
    token.full.slice(0, prefixEnd > 0 ? prefixEnd : 9) +
    '\u2022'.repeat(20) +
    token.full.slice(-4)
  return (
    <div className="rounded-lg border border-primary/40 bg-primary/5 p-5">
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2 text-sm">
          <Fingerprint className="size-4 text-primary" />
          <span className="font-medium">Token minted</span>
          <span className="text-muted-foreground">·</span>
          <span className="font-mono text-xs">{token.name}</span>
        </div>
        <div className="flex items-center gap-1.5 text-xs text-approval">
          <AlertTriangle className="size-3.5" />
          This is the only time we'll show it in full.
        </div>
      </div>

      <div className="mt-4 flex items-center gap-2 rounded-md border border-border bg-background px-4 py-3 font-mono text-sm">
        {/* `min-w-0` is required for flex-1 + truncate to actually
           clip — without it the span's intrinsic min-width keeps it
           from shrinking, and the long token pushes the Hide/Copy
           buttons past the parent. */}
        <span className="min-w-0 flex-1 truncate">
          {revealed ? token.full : masked}
        </span>
        <Button
          variant="outline"
          size="sm"
          onClick={() => setRevealed((r) => !r)}
          className="gap-1.5"
        >
          {revealed ? <EyeOff className="size-3.5" /> : <Eye className="size-3.5" />}
          {revealed ? 'Hide' : 'Reveal'}
        </Button>
        <Button
          variant="default"
          size="sm"
          onClick={async () => {
            await navigator.clipboard.writeText(token.full)
          }}
          className="gap-1.5"
        >
          <Copy className="size-3.5" />
          Copy
        </Button>
      </div>

      <div className="mt-3 flex items-center gap-4 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5">
          <Clock className="size-3.5" />
          Created just now
        </span>
        <span className="flex items-center gap-1.5">
          <KeyRound className="size-3.5" />
          {token.scopes.length} scope{token.scopes.length === 1 ? '' : 's'}
        </span>
        <button
          onClick={onDismiss}
          className="ml-auto text-muted-foreground hover:text-foreground"
        >
          Dismiss
        </button>
      </div>
    </div>
  )
}

function MintDialog({
  projectId,
  onSuccess,
}: {
  projectId: string
  onSuccess: (token: TokenMintResponse) => void
}) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [env, setEnv] = useState<'test' | 'live'>('live')
  const qc = useQueryClient()

  const mutation = useMutation({
    mutationFn: (body: Parameters<typeof api.mintToken>[0]) =>
      api.mintToken(body, projectId),
    onSuccess: (token) => {
      qc.invalidateQueries({ queryKey: ['tokens', projectId] })
      onSuccess(token)
      setOpen(false)
      setName('')
    },
  })

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        setOpen(o)
        if (!o) mutation.reset()
      }}
    >
      <DialogTrigger asChild>
        <Button className="gap-2">
          <Plus className="size-4" />
          Mint new token
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Mint dev token</DialogTitle>
          <DialogDescription>
            Tokens authenticate backend services to Hexgate. Use a clear name so you know
            where it's deployed.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="token-name">Name</Label>
            <Input
              id="token-name"
              placeholder="e.g. backend-prod"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
            />
          </div>

          <div className="space-y-1.5">
            <Label>Environment</Label>
            <div className="flex gap-2">
              {(['test', 'live'] as const).map((value) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setEnv(value)}
                  className={cn(
                    'flex-1 h-9 rounded-md border text-sm capitalize transition-colors',
                    env === value
                      ? 'border-primary bg-primary/10 text-primary'
                      : 'border-border text-muted-foreground hover:text-foreground',
                  )}
                >
                  {value}
                </button>
              ))}
            </div>
            <p className="text-[11px] text-muted-foreground">
              <span className="font-mono">fty_test_</span> keys read from a local policy file.
              <span className="font-mono">fty_live_</span> keys fetch signed bundles from the
              control plane.
            </p>
          </div>

          {mutation.isError && (
            <div className="rounded-md border border-deny/40 bg-deny/5 p-3 text-sm text-deny">
              {(mutation.error as Error).message}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button
            onClick={() => mutation.mutate({ name, env })}
            disabled={name.trim().length === 0 || mutation.isPending}
          >
            {mutation.isPending ? 'Minting…' : 'Mint token'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function TokensPage() {
  const [justMinted, setJustMinted] = useState<TokenMintResponse | null>(null)
  const scope = useProjectScoped()
  // ``enabled: !!scope.projectId`` keeps React Query quiet while we
  // wait for the active-project bootstrap; the cache key includes the
  // id so switching projects doesn't show stale rows.
  const tokens = useQuery({
    queryKey: ['tokens', scope.projectId],
    queryFn: () => api.listTokens(scope.projectId as string),
    enabled: !!scope.projectId,
  })
  const qc = useQueryClient()
  const revoke = useMutation({
    mutationFn: (tokenId: string) =>
      api.revokeToken(tokenId, scope.projectId as string),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ['tokens', scope.projectId] }),
  })

  if (scope.status === 'no-project') {
    return (
      <div className="max-w-[1400px] mx-auto">
        <h1 className="text-2xl font-semibold tracking-tight">Tokens</h1>
        <NoProjectEmptyState resource="tokens" />
      </div>
    )
  }

  return (
    <div className="max-w-[1400px] mx-auto">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Tokens</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Long-lived dev tokens for backend services. Never commit to source control.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="ghost" className="gap-2 text-muted-foreground">
            <BookOpen className="size-4" />
            Token docs
          </Button>
          {scope.projectId && (
            <MintDialog projectId={scope.projectId} onSuccess={setJustMinted} />
          )}
        </div>
      </div>

      {justMinted && (
        <div className="mt-6">
          <JustMintedBanner token={justMinted} onDismiss={() => setJustMinted(null)} />
        </div>
      )}

      <div className="mt-6 rounded-lg border border-border bg-card">
        <div className="flex items-center justify-between border-b border-border px-5 py-3">
          <div className="text-sm">
            Dev tokens <span className="text-muted-foreground">· {tokens.data?.length ?? 0}</span>
          </div>
          <div className="flex items-center gap-1 text-xs text-muted-foreground">
            <Button variant="ghost" size="sm" className="gap-1.5 text-xs">
              <Filter className="size-3.5" />
              Filter
            </Button>
            <Button variant="ghost" size="sm" className="gap-1.5 text-xs">
              <Clock className="size-3.5" />
              Last used
            </Button>
          </div>
        </div>

        {tokens.isLoading ? (
          <div className="p-12 text-center text-sm text-muted-foreground">Loading…</div>
        ) : !tokens.data || tokens.data.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
            <KeyRound className="size-12 text-muted-foreground/50" />
            <div className="text-sm font-medium">No tokens yet</div>
            <div className="max-w-xs text-xs text-muted-foreground">
              Mint your first dev token to let a backend service authenticate to Hexgate.
            </div>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-[10px] uppercase tracking-wider text-muted-foreground">
                <th className="px-5 py-2.5 text-left font-medium">Name</th>
                <th className="px-5 py-2.5 text-left font-medium">Token</th>
                <th className="px-5 py-2.5 text-left font-medium">Scopes</th>
                <th className="px-5 py-2.5 text-left font-medium">Created</th>
                <th className="px-5 py-2.5 text-left font-medium">Last used</th>
                <th className="px-5 py-2.5 w-24" />
              </tr>
            </thead>
            <tbody>
              {tokens.data.map((t) => {
                const isTest = t.masked.startsWith('fty_test_')
                return (
                  <tr
                    key={t.id}
                    className="border-b border-border/50 last:border-b-0 hover:bg-accent/50"
                  >
                    <td className="px-5 py-3">
                      <span className="flex items-center gap-2">
                        <KeyRound className="size-3.5 text-muted-foreground" />
                        <span className="font-medium">{t.name}</span>
                        {isTest && <Badge variant="approval">test</Badge>}
                        {!t.last_used_at && (
                          <Badge variant="outline">stale</Badge>
                        )}
                      </span>
                    </td>
                    <td className="px-5 py-3">
                      <span className="font-mono text-xs text-muted-foreground">{t.masked}</span>
                    </td>
                    <td className="px-5 py-3">
                      <span className="inline-flex flex-wrap gap-1">
                        {t.scopes.slice(0, 2).map((s) => (
                          <Badge key={s} variant="default">
                            {s}
                          </Badge>
                        ))}
                        {t.scopes.length > 2 && (
                          <Badge variant="outline">+{t.scopes.length - 2}</Badge>
                        )}
                      </span>
                    </td>
                    <td className="px-5 py-3 text-muted-foreground">
                      {formatCreated(t.created_at)}
                    </td>
                    <td
                      className={cn(
                        'px-5 py-3',
                        t.last_used_at ? 'text-muted-foreground' : 'text-approval',
                      )}
                    >
                      {formatRelative(t.last_used_at)}
                    </td>
                    <td className="px-5 py-3">
                      <div className="flex items-center justify-end gap-0.5">
                        <CopyButton value={t.masked} />
                        <Button
                          variant="ghost"
                          size="icon"
                          className="size-7 text-muted-foreground"
                          disabled
                          title="Rotate (coming soon)"
                        >
                          <RefreshCcw className="size-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="size-7 text-deny hover:text-deny hover:bg-deny/10"
                          onClick={() => revoke.mutate(t.id)}
                          title="Revoke"
                        >
                          <Trash2 className="size-3.5" />
                        </Button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
