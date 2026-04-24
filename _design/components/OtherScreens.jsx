// Audit, Tokens, Onboarding
const { Icon, Shell, DecisionBadge } = window;
const { useState } = React;

// ——— AUDIT ———————————————————————————————————————————
const AUDIT_EVENTS = [
  { ts: '2026-04-23 14:32:09.110', role: 'support', tool: 'refund_order', decision: 'deny', token: 'usr_8aF…2Qx', reason: 'amount > max_amount (50)', latency: 2.8 },
  { ts: '2026-04-23 14:31:42.003', role: 'support', tool: 'send_email', decision: 'allow', token: 'usr_8aF…2Qx', reason: '—', latency: 1.9 },
  { ts: '2026-04-23 14:31:41.521', role: 'support', tool: 'refund_order', decision: 'allow', token: 'usr_8aF…2Qx', reason: 'amount ≤ 50', latency: 2.1 },
  { ts: '2026-04-23 14:31:41.082', role: 'support', tool: 'lookup_customer', decision: 'allow', token: 'usr_8aF…2Qx', reason: '—', latency: 3.3 },
  { ts: '2026-04-23 14:31:40.884', role: 'billing', tool: 'issue_credit', decision: 'approval', token: 'usr_3kD…9Mn', reason: 'amount > 100', latency: 4.0 },
  { ts: '2026-04-23 14:30:12.221', role: 'support', tool: 'refund_order', decision: 'deny', token: 'usr_8aF…2Qx', reason: 'amount > max_amount (50)', latency: 2.4 },
  { ts: '2026-04-23 14:29:58.003', role: 'ops', tool: 'restart_service', decision: 'allow', token: 'dev_live_9F…', reason: '—', latency: 1.7 },
  { ts: '2026-04-23 14:29:44.112', role: 'support', tool: 'delete_account', decision: 'deny', token: 'usr_8aF…2Qx', reason: 'not in allowlist', latency: 1.8 },
  { ts: '2026-04-23 14:28:44.612', role: 'support', tool: 'send_email', decision: 'allow', token: 'usr_8aF…2Qx', reason: '—', latency: 2.2 },
  { ts: '2026-04-23 14:28:02.101', role: 'ops', tool: 'grant_admin', decision: 'deny', token: 'dev_live_9F…', reason: 'role not authorized', latency: 1.9 },
  { ts: '2026-04-23 14:27:33.442', role: 'analytics', tool: 'query_warehouse', decision: 'allow', token: 'usr_5tR…Lk9', reason: '—', latency: 5.6 },
  { ts: '2026-04-23 14:26:08.991', role: 'analytics', tool: 'export_data', decision: 'approval', token: 'usr_5tR…Lk9', reason: 'rows > 10,000', latency: 3.1 },
];

function AuditScreen() {
  const [sel, setSel] = useState(0);
  const evt = AUDIT_EVENTS[sel];
  return (
    <Shell active="audit">
      <div className="fty-page" style={{ maxWidth: 1400, paddingBottom: 0 }}>
        <div className="fty-page-hd">
          <div>
            <h1 className="fty-page-title">Audit</h1>
            <div className="fty-page-sub">Signed, append-only log of every policy decision. <span className="mono" style={{ color: 'hsl(var(--foreground))' }}>ed25519</span> signatures verified on ingest.</div>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="fty-btn ghost"><Icon name="fingerprint" size={13} />Verify chain</button>
            <button className="fty-btn secondary"><Icon name="download" size={13} />Export JSONL</button>
          </div>
        </div>

        {/* Filter bar */}
        <div style={{ display: 'flex', gap: 8, marginBottom: 16, alignItems: 'center', flexWrap: 'wrap' }}>
          <div style={{ position: 'relative', flex: '1 1 280px', maxWidth: 360 }}>
            <Icon name="search" size={13} style={{ position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)', color: 'hsl(var(--muted-foreground))' }} />
            <input className="fty-input" placeholder="Search tool, token, reason…" style={{ paddingLeft: 30 }} />
          </div>
          <select className="fty-select" defaultValue="last24">
            <option value="last24">Last 24 hours</option><option>Last 7 days</option><option>Custom range</option>
          </select>
          <select className="fty-select" defaultValue="all-roles"><option value="all-roles">All roles</option><option>support</option></select>
          <select className="fty-select" defaultValue="all-tools"><option value="all-tools">All tools</option></select>
          <div className="fty-seg" style={{ width: 'auto' }}>
            <button className="active"><Icon name="list" size={12} />All</button>
            <button style={{ color: 'hsl(var(--semantic-allow))' }}><Icon name="check" size={12} strokeWidth={2} />allow</button>
            <button style={{ color: 'hsl(var(--semantic-deny))' }}><Icon name="x" size={12} strokeWidth={2} />deny</button>
            <button style={{ color: 'hsl(var(--semantic-approval))' }}><Icon name="circle-dashed" size={12} strokeWidth={2} />approval</button>
          </div>
          <span style={{ fontSize: 12, color: 'hsl(var(--muted-foreground))', marginLeft: 'auto' }}>
            <span style={{ color: 'hsl(var(--foreground))' }}>12</span> of <span className="mono">18,673</span> events
          </span>
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 420px', borderTop: '1px solid hsl(var(--border))' }}>
        <div style={{ overflow: 'auto' }}>
          <table className="fty-table" style={{ marginBottom: 0 }}>
            <thead>
              <tr>
                <th style={{ width: 200 }}>Timestamp</th>
                <th>Role</th>
                <th>Tool</th>
                <th>Decision</th>
                <th>Token</th>
                <th>Reason</th>
                <th style={{ width: 70 }}>Latency</th>
              </tr>
            </thead>
            <tbody>
              {AUDIT_EVENTS.map((e, i) => (
                <tr key={i} onClick={() => setSel(i)} style={{ cursor: 'pointer', background: sel === i ? 'hsl(var(--primary) / 0.08)' : undefined }}>
                  <td className="mono" style={{ color: 'hsl(var(--muted-foreground))', fontSize: 12 }}>{e.ts}</td>
                  <td>{e.role}</td>
                  <td className="mono" style={{ fontSize: 12.5 }}>{e.tool}</td>
                  <td><DecisionBadge d={e.decision} /></td>
                  <td className="mono" style={{ fontSize: 12, color: 'hsl(var(--muted-foreground))' }}>{e.token}</td>
                  <td style={{ color: 'hsl(var(--muted-foreground))', fontSize: 12.5 }}>{e.reason}</td>
                  <td className="mono" style={{ fontSize: 12, color: 'hsl(var(--muted-foreground))' }}>{e.latency.toFixed(1)}ms</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Right panel */}
        <aside style={{ borderLeft: '1px solid hsl(var(--border))', background: 'hsl(var(--card))', padding: '20px 24px', overflow: 'auto' }}>
          <div style={{ fontSize: 11, color: 'hsl(var(--muted-foreground))', textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: 6 }}>Event</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 14 }}>
            <DecisionBadge d={evt.decision} />
            <span className="mono" style={{ fontSize: 12, color: 'hsl(var(--muted-foreground))' }}>evt_01HZ4K{sel}N8T</span>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '90px 1fr', rowGap: 8, fontSize: 13 }}>
            <KV k="When">{evt.ts}</KV>
            <KV k="Role">{evt.role}</KV>
            <KV k="Tool" mono>{evt.tool}</KV>
            <KV k="Token" mono muted>{evt.token}</KV>
            <KV k="Rule" mono>rl_refund_lt_50</KV>
            <KV k="Latency">{evt.latency.toFixed(1)} ms</KV>
            <KV k="Bundle" mono>v14</KV>
          </div>

          <div style={{ fontSize: 11, color: 'hsl(var(--muted-foreground))', textTransform: 'uppercase', letterSpacing: '0.04em', margin: '24px 0 8px' }}>Raw event</div>
          <pre style={{ margin: 0, padding: 12, background: 'hsl(var(--muted))', border: '1px solid hsl(var(--border))', borderRadius: 6, fontSize: 11.5, fontFamily: 'Geist Mono, monospace', lineHeight: 1.6, whiteSpace: 'pre-wrap', color: 'hsl(var(--muted-foreground))' }}>
{`{
  "event_id": "evt_01HZ4K${sel}N8T",
  "bundle_version": 14,
  "role": "${evt.role}",
  "tool": "${evt.tool}",
  `}<span style={{ color: evt.decision === 'allow' ? 'hsl(var(--semantic-allow))' : evt.decision === 'deny' ? 'hsl(var(--semantic-deny))' : 'hsl(var(--semantic-approval))' }}>{`"decision": "${evt.decision}",`}</span>{`
  "reason": "${evt.reason}",
  "latency_ms": ${evt.latency},
  "sig": "ed25519:8f3d…"
}`}
          </pre>

          <div style={{ fontSize: 11, color: 'hsl(var(--muted-foreground))', textTransform: 'uppercase', letterSpacing: '0.04em', margin: '20px 0 8px' }}>Chain</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
            <Icon name="check-circle-2" size={14} color="hsl(var(--semantic-allow))" />
            <span style={{ color: 'hsl(var(--semantic-allow))' }}>Signature verified</span>
            <span className="mono" style={{ color: 'hsl(var(--muted-foreground))', marginLeft: 4 }}>prev: evt_01HZ4K…N8S</span>
          </div>
        </aside>
      </div>
    </Shell>
  );
}

function KV({ k, children, mono, muted }) {
  return (
    <>
      <div style={{ color: 'hsl(var(--muted-foreground))', fontSize: 12 }}>{k}</div>
      <div className={mono ? 'mono' : ''} style={{ fontSize: mono ? 12.5 : 13, color: muted ? 'hsl(var(--muted-foreground))' : 'hsl(var(--foreground))' }}>{children}</div>
    </>
  );
}

// ——— TOKENS ———————————————————————————————————————————
function TokensScreen({ showMintedModal = true }) {
  const tokens = [
    { name: 'ci-deploy', id: 'fty_live_8F3d…k29P', created: 'Mar 12, 2026', lastUsed: '2m ago', scopes: ['mint_user_token', 'read_audit'], kind: 'dev' },
    { name: 'backend-prod', id: 'fty_live_92aQ…Lm44', created: 'Feb 28, 2026', lastUsed: '14s ago', scopes: ['mint_user_token'], kind: 'dev' },
    { name: 'analytics-etl', id: 'fty_live_3kX2…p8Zn', created: 'Feb 14, 2026', lastUsed: '1h ago', scopes: ['read_audit'], kind: 'dev' },
    { name: 'staging-box', id: 'fty_test_11Fc…Rz09', created: 'Jan 30, 2026', lastUsed: '3d ago', scopes: ['mint_user_token', 'read_audit', 'publish_bundle'], kind: 'test' },
    { name: 'legacy-worker', id: 'fty_live_77Mn…0pYq', created: 'Oct 04, 2025', lastUsed: 'Never', scopes: ['read_audit'], kind: 'dev', stale: true },
  ];
  return (
    <Shell active="tokens">
      <div className="fty-page">
        <div className="fty-page-hd">
          <div>
            <h1 className="fty-page-title">Tokens</h1>
            <div className="fty-page-sub">Long-lived dev tokens for backend services. Never commit to source control.</div>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="fty-btn ghost"><Icon name="book-open" size={13} />Token docs</button>
            <button className="fty-btn primary"><Icon name="plus" size={13} />Mint new token</button>
          </div>
        </div>

        {/* Just-minted callout */}
        {showMintedModal && (
          <div className="fty-card" style={{ padding: 0, marginBottom: 24, border: '1px solid hsl(var(--primary) / 0.5)', overflow: 'hidden' }}>
            <div style={{ padding: '14px 20px', background: 'hsl(var(--primary) / 0.08)', borderBottom: '1px solid hsl(var(--primary) / 0.3)', display: 'flex', alignItems: 'center', gap: 10 }}>
              <Icon name="fingerprint" size={16} color="hsl(var(--primary))" />
              <div style={{ fontSize: 13.5, fontWeight: 600 }}>Token minted · <span style={{ fontWeight: 400, color: 'hsl(var(--muted-foreground))' }}>production-api</span></div>
              <span style={{ marginLeft: 'auto', fontSize: 12, color: 'hsl(var(--semantic-approval))' }}>
                <Icon name="alert-triangle" size={12} strokeWidth={2} /> This is the only time we'll show it in full.
              </span>
            </div>
            <div style={{ padding: 20 }}>
              <div style={{ fontFamily: 'Geist Mono, monospace', fontSize: 15, padding: '14px 16px', background: 'hsl(var(--muted))', border: '1px solid hsl(var(--border))', borderRadius: 6, display: 'flex', alignItems: 'center' }}>
                <span>fty_live_8F3d</span><span style={{ color: 'hsl(var(--primary))' }}>2K9aP0xLmR4QvN8wYjT3zBcE7fHuD5iGs1M</span><span>k29P</span>
                <div style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
                  <button className="fty-btn secondary sm"><Icon name="eye-off" size={12} />Hide</button>
                  <button className="fty-btn primary sm"><Icon name="copy" size={12} />Copy</button>
                </div>
              </div>
              <div style={{ display: 'flex', gap: 20, marginTop: 14, fontSize: 12, color: 'hsl(var(--muted-foreground))' }}>
                <span><Icon name="clock" size={11} /> Created just now</span>
                <span><Icon name="shield-check" size={11} /> 3 scopes</span>
                <span><Icon name="calendar" size={11} /> No expiry</span>
                <span className="mono">sha256:4e9b…a2d1</span>
              </div>
            </div>
          </div>
        )}

        {/* Token table */}
        <div className="fty-card" style={{ padding: 0, overflow: 'hidden' }}>
          <div style={{ padding: '14px 20px', borderBottom: '1px solid hsl(var(--border))', display: 'flex', alignItems: 'center' }}>
            <div style={{ fontSize: 14, fontWeight: 600 }}>Dev tokens <span style={{ color: 'hsl(var(--muted-foreground))', fontWeight: 400 }}>· {tokens.length}</span></div>
            <div style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
              <button className="fty-btn ghost sm"><Icon name="filter" size={12} />Filter</button>
              <button className="fty-btn ghost sm"><Icon name="arrow-up-down" size={12} />Last used</button>
            </div>
          </div>
          <table className="fty-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Token</th>
                <th>Scopes</th>
                <th>Created</th>
                <th>Last used</th>
                <th style={{ width: 80 }}></th>
              </tr>
            </thead>
            <tbody>
              {tokens.map((t, i) => (
                <tr key={i}>
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <Icon name={t.kind === 'test' ? 'key-square' : 'key-round'} size={14} color={t.kind === 'test' ? 'hsl(var(--semantic-approval))' : 'hsl(var(--primary))'} />
                      <span style={{ fontWeight: 500 }}>{t.name}</span>
                      {t.kind === 'test' && <span className="fty-badge approval" style={{ height: 18, fontSize: 10 }}>test</span>}
                      {t.stale && <span className="fty-badge muted" style={{ height: 18, fontSize: 10 }}>stale</span>}
                    </div>
                  </td>
                  <td className="mono" style={{ fontSize: 12, color: 'hsl(var(--muted-foreground))' }}>{t.id}</td>
                  <td>
                    <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                      {t.scopes.slice(0, 2).map(s => <span key={s} className="fty-badge muted" style={{ fontSize: 10.5 }}>{s}</span>)}
                      {t.scopes.length > 2 && <span className="fty-badge muted" style={{ fontSize: 10.5 }}>+{t.scopes.length - 2}</span>}
                    </div>
                  </td>
                  <td style={{ fontSize: 12.5, color: 'hsl(var(--muted-foreground))' }}>{t.created}</td>
                  <td style={{ fontSize: 12.5, color: t.lastUsed === 'Never' ? 'hsl(var(--semantic-approval))' : 'hsl(var(--foreground))' }}>{t.lastUsed}</td>
                  <td>
                    <div style={{ display: 'flex', gap: 2, justifyContent: 'flex-end' }}>
                      <button className="fty-iconbtn" title="Copy prefix"><Icon name="copy" size={13} /></button>
                      <button className="fty-iconbtn" title="Rotate"><Icon name="rotate-cw" size={13} /></button>
                      <button className="fty-iconbtn" title="Revoke" style={{ color: 'hsl(var(--semantic-deny))' }}><Icon name="trash-2" size={13} /></button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </Shell>
  );
}

// ——— ONBOARDING ———————————————————————————————————————————
function OnboardingScreen() {
  return (
    <Shell active="home" noSidebar>
      <div style={{ display: 'grid', placeItems: 'center', height: '100%', padding: 40, background: 'hsl(var(--background))', position: 'relative' }}>
        {/* subtle grid backdrop */}
        <div style={{
          position: 'absolute', inset: 0,
          backgroundImage: 'radial-gradient(circle, hsl(var(--border) / 0.6) 1px, transparent 1px)',
          backgroundSize: '24px 24px',
          maskImage: 'radial-gradient(ellipse at center, black, transparent 75%)',
          WebkitMaskImage: 'radial-gradient(ellipse at center, black, transparent 75%)',
        }} />
        <div style={{ position: 'relative', textAlign: 'center', maxWidth: 520 }}>
          {/* Shield with glow */}
          <div style={{
            width: 96, height: 96, borderRadius: 24, margin: '0 auto 28px',
            display: 'grid', placeItems: 'center',
            background: 'linear-gradient(135deg, hsl(var(--primary)) 0%, hsl(226 78% 50%) 100%)',
            boxShadow: '0 0 0 1px hsl(var(--primary) / 0.4), 0 0 80px hsl(var(--primary) / 0.35), inset 0 1px 0 hsla(0 0% 100% / 0.2)',
          }}>
            <svg width="52" height="52" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
              <path d="m9 12 2 2 4-4"/>
            </svg>
          </div>

          <h1 style={{ fontSize: 36, fontWeight: 600, letterSpacing: '-0.03em', lineHeight: 1.1, margin: 0 }}>Welcome to Fortify</h1>
          <p style={{ fontSize: 15, color: 'hsl(var(--muted-foreground))', lineHeight: 1.55, margin: '14px auto 32px', maxWidth: 440 }}>
            Policy, identity, and audit for AI agents. Define what your agents can do, and prove what they did — signed, timestamped, append-only.
          </p>

          {/* Create project card */}
          <div className="fty-card" style={{ textAlign: 'left', padding: 24, marginBottom: 20 }}>
            <div style={{ fontSize: 11, color: 'hsl(var(--muted-foreground))', textTransform: 'uppercase', letterSpacing: '0.04em', marginBottom: 10 }}>Step 1 of 3 · Create project</div>
            <div style={{ marginBottom: 14 }}>
              <div style={{ fontSize: 12, color: 'hsl(var(--muted-foreground))', marginBottom: 6 }}>Project name</div>
              <input className="fty-input" defaultValue="support-bot" style={{ height: 36, fontSize: 14 }} />
            </div>
            <div style={{ marginBottom: 18 }}>
              <div style={{ fontSize: 12, color: 'hsl(var(--muted-foreground))', marginBottom: 6 }}>Starting template</div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
                <TemplateCard icon="message-square-code" title="Support agent" sub="4 roles · refund, email, lookup" selected />
                <TemplateCard icon="folder-key" title="Empty project" sub="Start from zero" />
              </div>
            </div>
            <button className="fty-btn primary lg" style={{ width: '100%', justifyContent: 'center' }}>
              Create project <Icon name="arrow-right" size={14} strokeWidth={2} />
            </button>
          </div>

          <div style={{ display: 'flex', justifyContent: 'center', gap: 24, fontSize: 12, color: 'hsl(var(--muted-foreground))' }}>
            <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}><Icon name="shield-check" size={12} color="hsl(var(--semantic-allow))" />SOC 2 Type II</span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}><Icon name="fingerprint" size={12} />Ed25519 signed audit</span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}><Icon name="server" size={12} />3-region control plane</span>
          </div>
        </div>
      </div>
    </Shell>
  );
}

function TemplateCard({ icon, title, sub, selected }) {
  return (
    <div style={{
      padding: 12, borderRadius: 8,
      border: `1px solid ${selected ? 'hsl(var(--primary))' : 'hsl(var(--border))'}`,
      background: selected ? 'hsl(var(--primary) / 0.08)' : 'hsl(var(--muted))',
      cursor: 'pointer', display: 'flex', gap: 10,
      boxShadow: selected ? '0 0 0 3px hsl(var(--primary) / 0.15)' : 'none',
    }}>
      <div style={{ width: 32, height: 32, borderRadius: 6, background: 'hsl(var(--secondary))', display: 'grid', placeItems: 'center', flexShrink: 0 }}>
        <Icon name={icon} size={16} color={selected ? 'hsl(var(--primary))' : 'hsl(var(--foreground))'} />
      </div>
      <div>
        <div style={{ fontSize: 13, fontWeight: 500 }}>{title}</div>
        <div style={{ fontSize: 11.5, color: 'hsl(var(--muted-foreground))', marginTop: 2 }}>{sub}</div>
      </div>
      {selected && <Icon name="check" size={14} strokeWidth={2.5} color="hsl(var(--primary))" style={{ marginLeft: 'auto' }} />}
    </div>
  );
}

Object.assign(window, { AuditScreen, TokensScreen, OnboardingScreen });
