// Dashboard home
const { Icon, Shell } = window;

function Sparkline({ data, color = 'hsl(var(--primary))', height = 36, width = 160 }) {
  const max = Math.max(...data);
  const min = Math.min(...data);
  const range = max - min || 1;
  const pts = data.map((v, i) => {
    const x = (i / (data.length - 1)) * width;
    const y = height - ((v - min) / range) * height;
    return `${x},${y}`;
  }).join(' ');
  const areaPts = `0,${height} ${pts} ${width},${height}`;
  return (
    <svg width={width} height={height} style={{ display: 'block' }}>
      <polygon points={areaPts} fill={color} opacity="0.1" />
      <polyline points={pts} fill="none" stroke={color} strokeWidth="1.5" />
    </svg>
  );
}

function KpiCard({ label, icon, value, sub, color, spark, sparkColor }) {
  return (
    <div className="fty-card">
      <div className="fty-kpi">
        <div className="fty-kpi-label"><Icon name={icon} size={13} /><span>{label}</span></div>
        <div className={`fty-kpi-val ${color || ''}`}>{value}</div>
        <div className="fty-kpi-sub">{sub}</div>
        {spark && <div className="fty-kpi-spark"><Sparkline data={spark} color={sparkColor} /></div>}
      </div>
    </div>
  );
}

const RECENT_EVENTS = [
  { ts: '14:32:08.412', role: 'support', tool: 'refund_order', decision: 'allow', token: 'usr_8aF…2Qx', reason: 'amount ≤ 50' },
  { ts: '14:31:55.109', role: 'support', tool: 'lookup_customer', decision: 'allow', token: 'usr_8aF…2Qx', reason: '—' },
  { ts: '14:31:40.884', role: 'billing', tool: 'issue_credit', decision: 'approval', token: 'usr_3kD…9Mn', reason: 'amount > 100' },
  { ts: '14:30:12.221', role: 'support', tool: 'refund_order', decision: 'deny', token: 'usr_8aF…2Qx', reason: 'amount > max_amount (50)' },
  { ts: '14:29:58.003', role: 'ops', tool: 'restart_service', decision: 'allow', token: 'dev_live_9F…', reason: '—' },
  { ts: '14:28:44.612', role: 'support', tool: 'send_email', decision: 'allow', token: 'usr_8aF…2Qx', reason: '—' },
  { ts: '14:28:02.101', role: 'support', tool: 'delete_account', decision: 'deny', token: 'usr_8aF…2Qx', reason: 'not in allowlist' },
  { ts: '14:27:33.442', role: 'analytics', tool: 'query_warehouse', decision: 'allow', token: 'usr_5tR…Lk9', reason: '—' },
];

function DecisionBadge({ d }) {
  if (d === 'allow') return <span className="fty-badge allow"><Icon name="check" size={11} strokeWidth={2} />allow</span>;
  if (d === 'deny') return <span className="fty-badge deny"><Icon name="x" size={11} strokeWidth={2} />deny</span>;
  return <span className="fty-badge approval"><Icon name="circle-dashed" size={11} strokeWidth={2} />approval</span>;
}

function DashboardScreen() {
  return (
    <Shell active="home">
      <div className="fty-page">
        <div className="fty-page-hd">
          <div>
            <h1 className="fty-page-title">Overview</h1>
            <div className="fty-page-sub">Live view of policy decisions, agents, and the active bundle for <span className="mono" style={{ color: 'hsl(var(--foreground))' }}>support-bot</span>.</div>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="fty-btn ghost"><Icon name="copy" size={13} />Copy project key</button>
            <button className="fty-btn secondary"><Icon name="message-square-code" size={13} />Playground</button>
            <button className="fty-btn primary"><Icon name="share-2" size={13} />Open graph</button>
          </div>
        </div>

        {/* KPIs */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 16, marginBottom: 24 }}>
          <KpiCard label="Allowed · 24h" icon="check" value="18,412" color="allow" sub="▲ 6.2% vs yesterday" spark={[12,18,14,22,28,30,24,34,40,38,46,52]} sparkColor="hsl(var(--semantic-allow))" />
          <KpiCard label="Denied · 24h" icon="x" value="247" color="deny" sub="▼ 12% vs yesterday" spark={[6,9,5,8,12,10,7,11,6,8,5,4]} sparkColor="hsl(var(--semantic-deny))" />
          <KpiCard label="Approval queue" icon="circle-dashed" value="14" color="approval" sub="3 awaiting > 5 min" spark={[2,3,4,2,5,6,8,7,10,9,12,14]} sparkColor="hsl(var(--semantic-approval))" />
          <KpiCard label="Active agents" icon="bot" value="32" sub="4 roles · 14 tools" spark={[18,20,22,24,26,28,29,30,30,31,32,32]} sparkColor="hsl(var(--primary))" />
        </div>

        {/* Bundle + quick actions */}
        <div style={{ display: 'grid', gridTemplateColumns: '1.5fr 1fr', gap: 16, marginBottom: 24 }}>
          <div className="fty-card pad-lg">
            <div className="fty-card-hd">
              <div>
                <div className="fty-card-title">Active bundle</div>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginTop: 6 }}>
                  <div style={{ fontSize: 22, fontWeight: 600, letterSpacing: '-0.01em' }} className="mono">v14</div>
                  <span className="fty-badge live"><span className="fty-dot-live" />serving</span>
                  <span style={{ fontSize: 12, color: 'hsl(var(--muted-foreground))' }}>pushed 4m ago by <b style={{ color: 'hsl(var(--foreground))', fontWeight: 500 }}>m.greene</b></span>
                </div>
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <button className="fty-btn ghost sm"><Icon name="history" size={12} />History</button>
                <button className="fty-btn secondary sm"><Icon name="file-edit" size={12} />Open draft</button>
              </div>
            </div>
            <div className="fty-code" style={{ marginTop: 4 }}>
              <span style={{ color: 'hsl(var(--muted-foreground))' }}>sha256:</span>
              <span>8f3d2a94b17e5c0a4e9b8f1d6c3e2a7b9d0f4c8a1e5b2d6f9c3e7a4b8d1f0e6c</span>
              <Icon name="copy" size={13} style={{ marginLeft: 'auto', cursor: 'pointer', color: 'hsl(var(--muted-foreground))' }} />
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 0, marginTop: 16, borderTop: '1px solid hsl(var(--border))', paddingTop: 16 }}>
              <Stat label="Roles" value="4" />
              <Stat label="Tools" value="14" />
              <Stat label="Allow rules" value="26" />
              <Stat label="Approvals" value="3" />
            </div>
          </div>

          <div className="fty-card pad-lg">
            <div className="fty-card-title" style={{ marginBottom: 14 }}>Quick actions</div>
            <QuickRow icon="plus" title="Mint user token" sub="For a new end-user session" />
            <QuickRow icon="upload" title="Publish draft" sub="2 changes since v14" badge={<span className="fty-badge approval"><Icon name="file-edit" size={10} />draft</span>} />
            <QuickRow icon="download" title="Export audit" sub="Last 24h as JSONL" />
            <QuickRow icon="book-open" title="Integrate SDK" sub="Python · Node · Go" last />
          </div>
        </div>

        {/* Recent events + top tools */}
        <div style={{ display: 'grid', gridTemplateColumns: '1.7fr 1fr', gap: 16 }}>
          <div className="fty-card" style={{ padding: 0, overflow: 'hidden' }}>
            <div style={{ padding: '16px 20px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', borderBottom: '1px solid hsl(var(--border))' }}>
              <div>
                <div style={{ fontSize: 15, fontWeight: 600 }}>Recent decisions</div>
                <div style={{ fontSize: 12, color: 'hsl(var(--muted-foreground))', marginTop: 2 }}>Streaming · auto-refresh <span className="fty-dot-live" style={{ display: 'inline-block', verticalAlign: 'middle', marginLeft: 4 }} /></div>
              </div>
              <button className="fty-btn ghost sm">View all <Icon name="arrow-right" size={12} /></button>
            </div>
            <table className="fty-table">
              <thead>
                <tr>
                  <th style={{ width: 130 }}>Time</th>
                  <th>Role</th>
                  <th>Tool</th>
                  <th>Decision</th>
                  <th>Token</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {RECENT_EVENTS.map((e, i) => (
                  <tr key={i}>
                    <td className="mono" style={{ color: 'hsl(var(--muted-foreground))', fontSize: 12 }}>{e.ts}</td>
                    <td>{e.role}</td>
                    <td className="mono" style={{ fontSize: 12.5 }}>{e.tool}</td>
                    <td><DecisionBadge d={e.decision} /></td>
                    <td className="mono" style={{ fontSize: 12, color: 'hsl(var(--muted-foreground))' }}>{e.token}</td>
                    <td style={{ color: 'hsl(var(--muted-foreground))', fontSize: 12.5 }}>{e.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="fty-card pad-lg">
            <div className="fty-card-title" style={{ marginBottom: 14 }}>Top tools · 24h</div>
            {[
              { name: 'lookup_customer', allow: 8240, deny: 12 },
              { name: 'send_email', allow: 4102, deny: 3 },
              { name: 'refund_order', allow: 3180, deny: 188 },
              { name: 'issue_credit', allow: 1244, deny: 24 },
              { name: 'query_warehouse', allow: 1088, deny: 8 },
              { name: 'restart_service', allow: 58, deny: 12 },
            ].map((t, i) => {
              const total = t.allow + t.deny;
              const denyPct = (t.deny / total) * 100;
              return (
                <div key={i} style={{ marginBottom: 12 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 5, fontSize: 12.5 }}>
                    <span className="mono">{t.name}</span>
                    <span style={{ color: 'hsl(var(--muted-foreground))' }}>
                      <span style={{ color: 'hsl(var(--foreground))' }}>{total.toLocaleString()}</span> calls
                    </span>
                  </div>
                  <div style={{ display: 'flex', height: 4, borderRadius: 2, overflow: 'hidden', background: 'hsl(var(--secondary))' }}>
                    <div style={{ width: `${100-denyPct}%`, background: 'hsl(var(--semantic-allow))' }} />
                    <div style={{ width: `${denyPct}%`, background: 'hsl(var(--semantic-deny))' }} />
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </Shell>
  );
}

function Stat({ label, value }) {
  return (
    <div>
      <div style={{ fontSize: 11.5, color: 'hsl(var(--muted-foreground))', letterSpacing: '0.02em' }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 600, marginTop: 4, letterSpacing: '-0.01em' }}>{value}</div>
    </div>
  );
}

function QuickRow({ icon, title, sub, badge, last }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 12,
      padding: '10px 0',
      borderBottom: last ? '0' : '1px solid hsl(var(--border))',
      cursor: 'pointer',
    }}>
      <div style={{ width: 32, height: 32, borderRadius: 6, background: 'hsl(var(--secondary))', display: 'grid', placeItems: 'center' }}>
        <Icon name={icon} size={15} />
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 13.5, fontWeight: 500 }}>{title}</div>
        <div style={{ fontSize: 12, color: 'hsl(var(--muted-foreground))' }}>{sub}</div>
      </div>
      {badge}
      <Icon name="chevron-right" size={14} color="hsl(var(--muted-foreground))" />
    </div>
  );
}

window.DashboardScreen = DashboardScreen;
window.DecisionBadge = DecisionBadge;
window.Sparkline = Sparkline;
