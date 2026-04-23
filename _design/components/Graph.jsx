// Graph / Resource Map — hero screen
const { Icon, Shell } = window;
const { useState } = React;

// Nodes (absolute px positions within canvas)
const ROLES = [
  { id: 'r-support', type: 'role', title: 'support', x: 60, y: 120 },
  { id: 'r-billing', type: 'role', title: 'billing', x: 60, y: 240 },
  { id: 'r-ops', type: 'role', title: 'ops', x: 60, y: 360 },
  { id: 'r-analytics', type: 'role', title: 'analytics', x: 60, y: 480 },
];
const TOOLS = [
  { id: 't-lookup', type: 'tool', title: 'lookup_customer', sub: 'tool.crm.lookup', x: 500, y: 60 },
  { id: 't-refund', type: 'tool', title: 'refund_order', sub: 'tool.commerce.refund', x: 500, y: 160 },
  { id: 't-credit', type: 'tool', title: 'issue_credit', sub: 'tool.commerce.credit', x: 500, y: 260 },
  { id: 't-email', type: 'tool', title: 'send_email', sub: 'tool.comms.email', x: 500, y: 360 },
  { id: 't-delete', type: 'tool', title: 'delete_account', sub: 'tool.crm.delete', x: 500, y: 460 },
  { id: 't-restart', type: 'tool', title: 'restart_service', sub: 'tool.infra.restart', x: 500, y: 560 },
  { id: 't-query', type: 'tool', title: 'query_warehouse', sub: 'tool.data.query', x: 820, y: 100 },
  { id: 't-grant', type: 'tool', title: 'grant_admin', sub: 'tool.auth.grant', x: 820, y: 240 },
  { id: 't-export', type: 'tool', title: 'export_data', sub: 'tool.data.export', x: 820, y: 380 },
  { id: 't-deploy', type: 'tool', title: 'deploy_bundle', sub: 'tool.infra.deploy', x: 820, y: 520 },
];

// Edges
const EDGES = [
  { from: 'r-support', to: 't-lookup', kind: 'allow' },
  { from: 'r-support', to: 't-refund', kind: 'allow', label: 'amount ≤ 50', selected: true },
  { from: 'r-support', to: 't-email', kind: 'allow' },
  { from: 'r-support', to: 't-delete', kind: 'deny' },
  { from: 'r-billing', to: 't-credit', kind: 'approval', label: 'if amount > 100' },
  { from: 'r-billing', to: 't-refund', kind: 'allow' },
  { from: 'r-billing', to: 't-export', kind: 'approval' },
  { from: 'r-ops', to: 't-restart', kind: 'allow' },
  { from: 'r-ops', to: 't-grant', kind: 'deny' },
  { from: 'r-ops', to: 't-deploy', kind: 'approval' },
  { from: 'r-analytics', to: 't-query', kind: 'allow' },
  { from: 'r-analytics', to: 't-export', kind: 'approval' },
];

function findNode(id) {
  return [...ROLES, ...TOOLS].find(n => n.id === id);
}

function edgeColor(kind) {
  if (kind === 'allow') return 'hsl(var(--semantic-allow))';
  if (kind === 'deny') return 'hsl(var(--semantic-deny))';
  return 'hsl(var(--semantic-approval))';
}

function GraphNode({ n, selected, onClick }) {
  const isRole = n.type === 'role';
  return (
    <div
      className={`fty-node ${isRole ? 'role' : 'tool'} ${selected ? 'selected' : ''}`}
      style={{ left: n.x, top: n.y }}
      onClick={onClick}
    >
      <div className="fty-node-icon">
        <Icon name={isRole ? 'users' : 'wrench'} size={16} color={isRole ? 'hsl(var(--primary))' : 'hsl(var(--foreground))'} />
      </div>
      <div className="fty-node-txt">
        <div className="fty-node-title">{n.title}</div>
        {n.sub && <div className="fty-node-sub">{n.sub}</div>}
      </div>
    </div>
  );
}

function GraphEdges({ edges, selectedEdge, onEdgeClick }) {
  // Node dim: 180 x 56. From-right, to-left.
  const labels = [];
  const paths = edges.map((e, i) => {
    const a = findNode(e.from), b = findNode(e.to);
    const x1 = a.x + 180;
    const y1 = a.y + 28;
    const x2 = b.x;
    const y2 = b.y + 28;
    const mx = (x1 + x2) / 2;
    const d = `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
    const isSel = selectedEdge === i;
    const color = isSel ? 'hsl(var(--primary))' : edgeColor(e.kind);
    const width = isSel ? 3 : (e.label ? 2.5 : 2);
    const dash = e.kind === 'approval' ? '6 4' : undefined;
    if (e.label) {
      labels.push({ i, x: mx, y: (y1 + y2) / 2, label: e.label, kind: e.kind });
    }
    return (
      <g key={i} style={{ cursor: 'pointer' }} onClick={() => onEdgeClick(i)}>
        <path d={d} stroke="transparent" strokeWidth="14" fill="none" />
        <path d={d} stroke={color} strokeWidth={width} strokeDasharray={dash} fill="none" />
        <circle cx={x1} cy={y1} r={3} fill={color} />
        <circle cx={x2} cy={y2} r={3} fill={color} />
      </g>
    );
  });
  return { paths, labels };
}

function GraphInspector({ edge, onClose }) {
  if (!edge) return null;
  const [mode, setMode] = useState(edge.kind);
  const a = findNode(edge.from);
  const b = findNode(edge.to);
  return (
    <aside className="fty-inspector">
      <div className="fty-inspector-hd">
        <div>
          <div style={{ fontSize: 11, color: 'hsl(var(--muted-foreground))', letterSpacing: '0.04em', textTransform: 'uppercase', marginBottom: 4 }}>Edge</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 14, fontWeight: 600 }}>
            <span>{a.title}</span>
            <Icon name="arrow-right" size={14} color="hsl(var(--muted-foreground))" />
            <span className="mono" style={{ fontSize: 13 }}>{b.title}</span>
          </div>
        </div>
        <button className="fty-iconbtn" onClick={onClose}><Icon name="x" size={16} /></button>
      </div>
      <div className="fty-inspector-body">
        <div style={{ fontSize: 11, color: 'hsl(var(--muted-foreground))', letterSpacing: '0.04em', textTransform: 'uppercase', marginBottom: 8 }}>Mode</div>
        <div className="fty-seg" style={{ marginBottom: 20 }}>
          <button className={mode === 'allow' ? 'active allow' : ''} onClick={() => setMode('allow')}>
            <Icon name="check" size={12} strokeWidth={2} />allow
          </button>
          <button className={mode === 'approval' ? 'active approval' : ''} onClick={() => setMode('approval')}>
            <Icon name="circle-dashed" size={12} strokeWidth={2} />approval
          </button>
          <button className={mode === 'deny' ? 'active deny' : ''} onClick={() => setMode('deny')}>
            <Icon name="x" size={12} strokeWidth={2} />deny
          </button>
        </div>

        <div style={{ fontSize: 11, color: 'hsl(var(--muted-foreground))', letterSpacing: '0.04em', textTransform: 'uppercase', marginBottom: 8, display: 'flex', justifyContent: 'space-between' }}>
          <span>Constraints</span>
          <span className="mono" style={{ textTransform: 'none', letterSpacing: 0, color: 'hsl(var(--muted-foreground))' }}>CEL</span>
        </div>
        <div className="fty-card" style={{ padding: 12, marginBottom: 8 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
            <div className="mono" style={{ fontSize: 12.5, color: 'hsl(var(--foreground))', flex: 1 }}>amount</div>
            <select className="fty-select" defaultValue="≤" style={{ height: 26, padding: '0 22px 0 8px', fontSize: 12 }}>
              <option>≤</option><option>&lt;</option><option>==</option><option>&gt;</option><option>≥</option>
            </select>
            <input className="fty-input mono" defaultValue="50" style={{ height: 26, width: 80, fontSize: 12 }} />
            <button className="fty-iconbtn" style={{ width: 26, height: 26 }}><Icon name="x" size={12} /></button>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <div className="mono" style={{ fontSize: 12.5, color: 'hsl(var(--foreground))', flex: 1 }}>currency</div>
            <select className="fty-select" defaultValue="==" style={{ height: 26, padding: '0 22px 0 8px', fontSize: 12 }}>
              <option>==</option><option>!=</option><option>in</option>
            </select>
            <input className="fty-input mono" defaultValue="USD" style={{ height: 26, width: 80, fontSize: 12 }} />
            <button className="fty-iconbtn" style={{ width: 26, height: 26 }}><Icon name="x" size={12} /></button>
          </div>
        </div>
        <button className="fty-btn ghost sm"><Icon name="plus" size={12} />Add constraint</button>

        <div style={{ fontSize: 11, color: 'hsl(var(--muted-foreground))', letterSpacing: '0.04em', textTransform: 'uppercase', margin: '24px 0 8px' }}>Preview</div>
        <div className="fty-code" style={{ padding: 12, fontSize: 12, lineHeight: 1.55, display: 'block', whiteSpace: 'pre' }}>
          <span style={{ color: 'hsl(var(--semantic-allow))' }}>allow</span>{' '}<span style={{ color: 'hsl(var(--muted-foreground))' }}>when</span>{'\n'}
          {'  '}role == <span style={{ color: 'hsl(var(--semantic-approval))' }}>"support"</span>{'\n'}
          {'  '}tool == <span style={{ color: 'hsl(var(--semantic-approval))' }}>"refund_order"</span>{'\n'}
          {'  '}<span style={{ color: 'hsl(var(--muted-foreground))' }}>and</span>{'\n'}
          {'  '}args.amount &lt;= <span style={{ color: 'hsl(var(--primary))' }}>50</span>{'\n'}
          {'  '}args.currency == <span style={{ color: 'hsl(var(--semantic-approval))' }}>"USD"</span>
        </div>

        <div style={{ fontSize: 11, color: 'hsl(var(--muted-foreground))', letterSpacing: '0.04em', textTransform: 'uppercase', margin: '24px 0 8px' }}>Recent decisions · 24h</div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8 }}>
          <MiniStat label="Allowed" value="3,180" color="hsl(var(--semantic-allow))" />
          <MiniStat label="Denied" value="188" color="hsl(var(--semantic-deny))" />
          <MiniStat label="p50" value="3.2 ms" />
        </div>
      </div>
      <div className="fty-inspector-ft">
        <button className="fty-btn ghost">Cancel</button>
        <button className="fty-btn primary"><Icon name="check" size={13} strokeWidth={2} />Save rule</button>
      </div>
    </aside>
  );
}

function MiniStat({ label, value, color = 'hsl(var(--foreground))' }) {
  return (
    <div style={{ background: 'hsl(var(--muted))', border: '1px solid hsl(var(--border))', borderRadius: 6, padding: '8px 10px' }}>
      <div style={{ fontSize: 11, color: 'hsl(var(--muted-foreground))' }}>{label}</div>
      <div className="mono" style={{ fontSize: 14, fontWeight: 600, color, marginTop: 2 }}>{value}</div>
    </div>
  );
}

function GraphScreen() {
  const [selectedEdge, setSelectedEdge] = useState(1);
  const currentEdge = selectedEdge != null ? EDGES[selectedEdge] : null;
  const { paths, labels } = GraphEdges({ edges: EDGES, selectedEdge, onEdgeClick: setSelectedEdge });
  return (
    <Shell active="graph">
      <div className="fty-graph">
        {/* Status top-left */}
        <div className="fty-graph-status">
          <span className="fty-badge approval"><Icon name="file-edit" size={11} strokeWidth={2} />draft · 2 changes</span>
          <span style={{ fontSize: 12, color: 'hsl(var(--muted-foreground))' }}>vs <span className="mono" style={{ color: 'hsl(var(--foreground))' }}>v14</span></span>
          <button className="fty-btn ghost sm"><Icon name="git-compare" size={12} />Diff</button>
          <button className="fty-btn primary sm"><Icon name="upload" size={12} />Publish</button>
        </div>

        {/* Toolbar top-right */}
        <div className="fty-graph-toolbar">
          <button className="fty-btn ghost sm"><Icon name="plus" size={12} />Role</button>
          <button className="fty-btn ghost sm"><Icon name="plus" size={12} />Tool</button>
          <div style={{ width: 1, height: 16, background: 'hsl(var(--border))' }} />
          <button className="fty-btn ghost sm"><Icon name="filter" size={12} />All edges</button>
          <button className="fty-btn ghost sm"><Icon name="code" size={12} />Raw</button>
        </div>

        {/* SVG edges */}
        <svg style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }}>
          <g style={{ pointerEvents: 'auto' }}>{paths}</g>
        </svg>

        {/* Nodes */}
        {[...ROLES, ...TOOLS].map(n => (
          <GraphNode key={n.id} n={n}
            selected={currentEdge && (currentEdge.from === n.id || currentEdge.to === n.id)}
            onClick={() => {}} />
        ))}

        {/* Edge labels */}
        {labels.map(l => (
          <div key={l.i} className={`fty-edge-pill ${l.kind}`} style={{ left: l.x, top: l.y }}>
            {l.kind === 'allow' && <Icon name="check" size={9} strokeWidth={2.5} />}
            {l.kind === 'deny' && <Icon name="x" size={9} strokeWidth={2.5} />}
            {l.kind === 'approval' && <Icon name="circle-dashed" size={9} strokeWidth={2.5} />}
            {l.label}
          </div>
        ))}

        {/* Controls */}
        <div className="fty-graph-controls">
          <button title="Zoom in"><Icon name="plus" size={14} /></button>
          <button title="Zoom out"><Icon name="minus" size={14} /></button>
          <button title="Fit"><Icon name="maximize" size={14} /></button>
          <button title="Lock"><Icon name="lock" size={14} /></button>
        </div>

        {/* Minimap */}
        <div className="fty-minimap">
          <svg viewBox="0 0 1100 700" width="100%" height="100%" preserveAspectRatio="xMidYMid meet">
            {EDGES.map((e, i) => {
              const a = findNode(e.from), b = findNode(e.to);
              return <line key={i} x1={a.x+90} y1={a.y+28} x2={b.x+90} y2={b.y+28} stroke={edgeColor(e.kind)} strokeOpacity="0.5" strokeWidth="3" />;
            })}
            {[...ROLES, ...TOOLS].map(n => (
              <rect key={n.id} x={n.x} y={n.y} width="180" height="56" rx="8"
                fill={n.type === 'role' ? 'hsl(226 78% 65% / 0.4)' : 'hsl(222 20% 14%)'}
                stroke="hsl(222 20% 25%)" strokeWidth="2" />
            ))}
            <rect x="20" y="20" width="1060" height="660" fill="none" stroke="hsl(var(--primary))" strokeWidth="4" strokeDasharray="12 8" opacity="0.6" />
          </svg>
        </div>

        {/* Inspector */}
        <GraphInspector edge={currentEdge} onClose={() => setSelectedEdge(null)} />
      </div>
    </Shell>
  );
}

window.GraphScreen = GraphScreen;
