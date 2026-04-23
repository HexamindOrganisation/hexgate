// Playground — three-column
const { Icon, Shell } = window;
const { useState } = React;

function PlaygroundScreen() {
  const [tab, setTab] = useState('decisions');
  return (
    <Shell active="playground">
      <div style={{ display: 'grid', gridTemplateColumns: '280px 1fr 400px', height: '100%', minHeight: 0 }}>
        {/* LEFT: session config */}
        <aside style={{ borderRight: '1px solid hsl(var(--border))', padding: '20px 18px', overflow: 'auto', background: 'hsl(var(--card))' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
            <Icon name="radio" size={14} color="hsl(var(--semantic-allow))" />
            <span className="fty-badge live"><span className="fty-dot-live" />connected</span>
          </div>
          <div style={{ fontSize: 20, fontWeight: 600, letterSpacing: '-0.01em', marginBottom: 2 }}>Playground</div>
          <div style={{ fontSize: 12, color: 'hsl(var(--muted-foreground))', marginBottom: 20 }}>Simulate an agent session against <span className="mono" style={{ color: 'hsl(var(--foreground))' }}>v14</span>.</div>

          <Label icon="users">Role</Label>
          <select className="fty-select" defaultValue="support" style={{ width: '100%', height: 32 }}>
            <option>support</option><option>billing</option><option>ops</option><option>analytics</option>
          </select>

          <Label icon="ticket">User token</Label>
          <div className="fty-code" style={{ padding: '8px 36px 8px 10px', fontSize: 11.5 }}>
            usr_8aF3…2Qx4
            <Icon name="copy" size={12} style={{ position: 'absolute', right: 8, color: 'hsl(var(--muted-foreground))' }} />
          </div>
          <div style={{ fontSize: 11, color: 'hsl(var(--muted-foreground))', marginTop: 4 }}>Expires in <span className="mono" style={{ color: 'hsl(var(--semantic-approval))' }}>4m 32s</span></div>

          <Label icon="bot">Model</Label>
          <select className="fty-select" defaultValue="claude-sonnet-4.5" style={{ width: '100%', height: 32 }}>
            <option>claude-sonnet-4.5</option><option>claude-opus-4</option><option>gpt-4.1</option>
          </select>

          <Label icon="sliders">Max tokens</Label>
          <input className="fty-input mono" defaultValue="4096" />

          <Label icon="thermometer">Temperature</Label>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <input type="range" min="0" max="1" step="0.1" defaultValue="0.2" style={{ flex: 1, accentColor: 'hsl(var(--primary))' }} />
            <span className="mono" style={{ fontSize: 12, width: 28 }}>0.2</span>
          </div>

          <div style={{ marginTop: 24, paddingTop: 20, borderTop: '1px solid hsl(var(--border))' }}>
            <Label icon="wrench">Tools attached</Label>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {['lookup_customer', 'refund_order', 'send_email', 'delete_account'].map(t => (
                <div key={t} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5 }}>
                  <Icon name={t === 'delete_account' ? 'x' : 'check'} size={12} strokeWidth={2} color={t === 'delete_account' ? 'hsl(var(--semantic-deny))' : 'hsl(var(--semantic-allow))'} />
                  <span className="mono">{t}</span>
                </div>
              ))}
            </div>
          </div>

          <button className="fty-btn secondary" style={{ marginTop: 20, width: '100%' }}><Icon name="rotate-ccw" size={13} />Reset session</button>
        </aside>

        {/* CENTER: chat */}
        <section style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
          <div style={{ padding: '14px 20px', borderBottom: '1px solid hsl(var(--border))', display: 'flex', alignItems: 'center', gap: 10 }}>
            <Icon name="message-square-code" size={15} color="hsl(var(--muted-foreground))" />
            <span style={{ fontSize: 13.5, fontWeight: 500 }}>Session</span>
            <span className="mono" style={{ fontSize: 12, color: 'hsl(var(--muted-foreground))' }}>sess_01HZ4…Nk</span>
            <div style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
              <button className="fty-btn ghost sm"><Icon name="share-2" size={12} />Share</button>
              <button className="fty-btn ghost sm"><Icon name="code" size={12} />Export cURL</button>
            </div>
          </div>

          <div style={{ flex: 1, overflow: 'auto' }}>
            <Msg who="user" avatar="MG" time="14:31:40">
              <div>Hey, can you refund order <span className="mono">#A-8824</span> for $38 and then email the customer?</div>
            </Msg>

            <Msg who="agent" avatar="" time="14:31:41">
              <div>I'll look up the order first, then process the refund.</div>
              <ToolCall tool="lookup_customer" args={`{ "order_id": "A-8824" }`} result={`{ "customer_id": "c_912", "email": "mira@acme.co", "order_total": 38 }`} decision="allow" />
              <ToolCall tool="refund_order" args={`{ "order_id": "A-8824", "amount": 38, "currency": "USD" }`} result={`{ "refund_id": "rf_2K9", "status": "refunded" }`} decision="allow" reason="amount ≤ 50" />
              <div style={{ marginTop: 10 }}>Refund issued. Sending the confirmation now.</div>
              <ToolCall tool="send_email" args={`{ "to": "mira@acme.co", "template": "refund_confirmed" }`} result={`{ "message_id": "ml_8Y1" }`} decision="allow" />
              <div style={{ marginTop: 10 }}>Done — refunded $38 to the original card, confirmation emailed to <span className="mono">mira@acme.co</span>.</div>
            </Msg>

            <Msg who="user" avatar="MG" time="14:32:08">
              <div>Actually, refund order <span className="mono">#A-8901</span> too — that one's $240.</div>
            </Msg>

            <Msg who="agent" avatar="" time="14:32:09">
              <ToolCall tool="refund_order" args={`{ "order_id": "A-8901", "amount": 240, "currency": "USD" }`} decision="deny" reason="amount > max_amount (50)" />
              <div style={{ marginTop: 10 }}>That refund was blocked — your role can only refund up to $50. I can escalate this to billing for approval if you'd like.</div>
            </Msg>
          </div>

          <div style={{ padding: 16, borderTop: '1px solid hsl(var(--border))' }}>
            <div style={{ position: 'relative' }}>
              <textarea
                placeholder="Ask the agent to do something…"
                style={{
                  width: '100%', minHeight: 64, padding: '12px 100px 12px 14px',
                  background: 'hsl(var(--muted))', border: '1px solid hsl(var(--border))',
                  borderRadius: 8, color: 'hsl(var(--foreground))', fontSize: 13.5,
                  fontFamily: 'inherit', resize: 'none', outline: 'none',
                }}
                defaultValue=""
              />
              <div style={{ position: 'absolute', right: 10, bottom: 10, display: 'flex', gap: 6 }}>
                <button className="fty-iconbtn"><Icon name="paperclip" size={14} /></button>
                <button className="fty-btn primary sm"><Icon name="arrow-up" size={13} strokeWidth={2} />Send</button>
              </div>
            </div>
            <div style={{ display: 'flex', gap: 16, marginTop: 8, fontSize: 11, color: 'hsl(var(--muted-foreground))' }}>
              <span><kbd style={kbd}>⌘</kbd><kbd style={kbd}>↵</kbd> send</span>
              <span><kbd style={kbd}>⌘</kbd><kbd style={kbd}>K</kbd> commands</span>
              <span style={{ marginLeft: 'auto' }}>3 tool calls · 2 allow · 1 deny</span>
            </div>
          </div>
        </section>

        {/* RIGHT: telemetry */}
        <aside style={{ borderLeft: '1px solid hsl(var(--border))', background: 'hsl(var(--card))', display: 'flex', flexDirection: 'column', minHeight: 0 }}>
          <div style={{ padding: '12px 16px', borderBottom: '1px solid hsl(var(--border))', display: 'flex', alignItems: 'center', gap: 10 }}>
            <div className="fty-tabs">
              <button className={tab === 'decisions' ? 'active' : ''} onClick={() => setTab('decisions')}><Icon name="shield-check" size={12} />Decisions</button>
              <button className={tab === 'audit' ? 'active' : ''} onClick={() => setTab('audit')}><Icon name="scroll-text" size={12} />Audit</button>
              <button className={tab === 'calls' ? 'active' : ''} onClick={() => setTab('calls')}><Icon name="wrench" size={12} />Calls</button>
            </div>
            <div style={{ marginLeft: 'auto', fontSize: 11, color: 'hsl(var(--muted-foreground))', display: 'flex', alignItems: 'center', gap: 5 }}>
              <span className="fty-dot-live" /> live
            </div>
          </div>
          <div style={{ overflow: 'auto', flex: 1 }}>
            {tab === 'decisions' && (
              <>
                <TelRow ts="14:32:09.110" tool="refund_order" d="deny" reason="amount > 50" />
                <TelRow ts="14:31:42.003" tool="send_email" d="allow" reason="role allow" />
                <TelRow ts="14:31:41.521" tool="refund_order" d="allow" reason="amount ≤ 50" />
                <TelRow ts="14:31:41.082" tool="lookup_customer" d="allow" reason="role allow" />
                <TelRow ts="14:29:55.441" tool="lookup_customer" d="allow" reason="role allow" />
                <TelRow ts="14:29:44.112" tool="delete_account" d="deny" reason="not in allowlist" />
              </>
            )}
            {tab === 'audit' && (
              <pre style={{ margin: 0, padding: 16, fontSize: 11.5, fontFamily: 'Geist Mono, monospace', lineHeight: 1.6, color: 'hsl(var(--muted-foreground))', whiteSpace: 'pre-wrap' }}>
{`{
  "event_id": "evt_01HZ4K...",
  "ts": "2026-04-23T14:32:09.110Z",
  "bundle_version": 14,
  "role": "support",
  "tool": "refund_order",
  "args": {
    "order_id": "A-8901",
    "amount": 240,
    "currency": "USD"
  },
  `}<span style={{ color: 'hsl(var(--semantic-deny))' }}>{`"decision": "deny",`}</span>{`
  `}<span style={{ color: 'hsl(var(--semantic-deny))' }}>{`"reason": "amount > max_amount (50)",`}</span>{`
  "rule_id": "rl_refund_lt_50",
  "latency_ms": 2.8,
  "token_id": "usr_8aF...",
  "sig": "ed25519:8f3d2a94..."
}`}
              </pre>
            )}
            {tab === 'calls' && (
              <div style={{ padding: 16 }}>
                <CallTree />
              </div>
            )}
          </div>
        </aside>
      </div>
    </Shell>
  );
}

const kbd = {
  padding: '1px 5px',
  border: '1px solid hsl(var(--border))',
  borderRadius: 3,
  fontFamily: 'Geist Mono, monospace',
  fontSize: 10,
  background: 'hsl(var(--card))',
  marginRight: 2,
};

function Label({ icon, children }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: 'hsl(var(--muted-foreground))', textTransform: 'uppercase', letterSpacing: '0.04em', marginTop: 16, marginBottom: 6 }}>
      <Icon name={icon} size={11} />{children}
    </div>
  );
}

function Msg({ who, avatar, time, children }) {
  return (
    <div className="fty-msg">
      <div className={`fty-msg-avatar ${who}`}>
        {who === 'agent'
          ? <Icon name="bot" size={14} />
          : <span style={{ fontWeight: 600 }}>{avatar}</span>}
      </div>
      <div className="fty-msg-body">
        <div className="fty-msg-who">
          <span style={{ color: 'hsl(var(--foreground))', fontWeight: 500 }}>{who === 'agent' ? 'agent' : 'you'}</span>
          {who === 'agent' && <span className="fty-badge muted" style={{ height: 16, fontSize: 10 }}>claude-sonnet-4.5</span>}
          <span className="mono" style={{ marginLeft: 'auto', fontSize: 11 }}>{time}</span>
        </div>
        <div className="fty-msg-txt">{children}</div>
      </div>
    </div>
  );
}

function ToolCall({ tool, args, result, decision, reason }) {
  const denied = decision === 'deny';
  return (
    <div className="fty-tool-call">
      <div className="fty-tool-call-hd">
        <Icon name="wrench" size={12} color="hsl(var(--muted-foreground))" />
        <span style={{ color: 'hsl(var(--foreground))' }}>{tool}</span>
        <span style={{ marginLeft: 'auto' }}>
          {decision === 'allow' && <span className="fty-badge allow"><Icon name="check" size={10} strokeWidth={2} />allow</span>}
          {decision === 'deny' && <span className="fty-badge deny"><Icon name="x" size={10} strokeWidth={2} />deny</span>}
        </span>
      </div>
      <div className="fty-tool-call-body">
        <div style={{ color: 'hsl(var(--muted-foreground))', fontSize: 10.5, marginBottom: 2 }}>ARGS</div>
        <div style={{ color: 'hsl(var(--foreground))' }}>{args}</div>
        {result && !denied && (
          <>
            <div style={{ color: 'hsl(var(--muted-foreground))', fontSize: 10.5, margin: '8px 0 2px' }}>RESULT</div>
            <div style={{ color: 'hsl(var(--semantic-allow))' }}>{result}</div>
          </>
        )}
        {reason && (
          <div style={{ marginTop: 8, fontSize: 11, color: denied ? 'hsl(var(--semantic-deny))' : 'hsl(var(--muted-foreground))' }}>
            {denied ? '✗ ' : '→ '}{reason}
          </div>
        )}
      </div>
    </div>
  );
}

function TelRow({ ts, tool, d, reason }) {
  return (
    <div className="fty-tel-row">
      <span className="ts">{ts}</span>
      {d === 'allow' && <Icon name="check" size={12} strokeWidth={2} color="hsl(var(--semantic-allow))" />}
      {d === 'deny' && <Icon name="x" size={12} strokeWidth={2} color="hsl(var(--semantic-deny))" />}
      {d === 'approval' && <Icon name="circle-dashed" size={12} strokeWidth={2} color="hsl(var(--semantic-approval))" />}
      <span className="tool">{tool}</span>
      <span className="reason">{reason}</span>
    </div>
  );
}

function CallTree() {
  return (
    <div style={{ fontFamily: 'Geist Mono, monospace', fontSize: 12, lineHeight: 1.7 }}>
      <div>▾ session <span style={{ color: 'hsl(var(--muted-foreground))' }}>sess_01HZ4…</span></div>
      <div style={{ paddingLeft: 16 }}>
        <div>▾ turn 1 <span style={{ color: 'hsl(var(--muted-foreground))' }}>· 2.1s</span></div>
        <div style={{ paddingLeft: 16 }}>
          <div>├─ <span style={{ color: 'hsl(var(--semantic-allow))' }}>✓</span> lookup_customer <span style={{ color: 'hsl(var(--muted-foreground))' }}>· 3ms</span></div>
          <div>├─ <span style={{ color: 'hsl(var(--semantic-allow))' }}>✓</span> refund_order <span style={{ color: 'hsl(var(--muted-foreground))' }}>· 2ms</span></div>
          <div>└─ <span style={{ color: 'hsl(var(--semantic-allow))' }}>✓</span> send_email <span style={{ color: 'hsl(var(--muted-foreground))' }}>· 4ms</span></div>
        </div>
        <div style={{ marginTop: 4 }}>▾ turn 2 <span style={{ color: 'hsl(var(--muted-foreground))' }}>· 0.4s</span></div>
        <div style={{ paddingLeft: 16 }}>
          <div>└─ <span style={{ color: 'hsl(var(--semantic-deny))' }}>✗</span> refund_order <span style={{ color: 'hsl(var(--muted-foreground))' }}>· denied</span></div>
          <div style={{ paddingLeft: 20, color: 'hsl(var(--semantic-deny))', fontSize: 11 }}>amount &gt; max_amount (50)</div>
        </div>
      </div>
    </div>
  );
}

window.PlaygroundScreen = PlaygroundScreen;
