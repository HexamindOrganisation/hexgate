// Fortify shell: topbar + sidebar scaffold
const { useState } = React;

function Icon({ name, size = 16, strokeWidth = 1.5, color = 'currentColor', style = {} }) {
  return <i data-lucide={name} style={{ width: size, height: size, color, strokeWidth, ...style }} />;
}

function TopBar({ project = 'support-bot', env = 'production' }) {
  return (
    <div className="fty-topbar">
      <div className="fty-logo">
        <div className="fty-logo-mark">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
          </svg>
        </div>
        <span>Fortify</span>
      </div>
      <div style={{ width: 1, height: 20, background: 'hsl(var(--border))' }} />
      <div className="fty-proj">
        <div className="fty-proj-dot" />
        <span style={{ color: 'hsl(var(--muted-foreground))', fontSize: 12 }}>Project</span>
        <span style={{ fontWeight: 500 }}>{project}</span>
        <span className="mono" style={{ color: 'hsl(var(--muted-foreground))', fontSize: 11 }}>· {env}</span>
        <Icon name="chevrons-up-down" size={13} color="hsl(var(--muted-foreground))" />
      </div>
      <div className="fty-topbar-right">
        <div className="fty-iconbtn" title="Docs"><Icon name="book-open" /></div>
        <div className="fty-iconbtn" title="Command"><Icon name="command" /></div>
        <div className="fty-iconbtn" title="Notifications"><Icon name="bell" /></div>
        <div style={{ width: 1, height: 20, background: 'hsl(var(--border))', margin: '0 4px' }} />
        <div className="fty-user">MG</div>
      </div>
    </div>
  );
}

const NAV = [
  { id: 'home', label: 'Dashboard', icon: 'layout-dashboard' },
  { id: 'graph', label: 'Resource map', icon: 'share-2' },
  { id: 'playground', label: 'Playground', icon: 'message-square-code' },
  { id: 'audit', label: 'Audit', icon: 'scroll-text', badge: '24h' },
  { id: 'tokens', label: 'Tokens', icon: 'key-round' },
  { id: 'settings', label: 'Settings', icon: 'settings-2' },
];

function Sidebar({ active = 'home' }) {
  return (
    <aside className="fty-sidebar">
      <div className="fty-nav-section">Workspace</div>
      {NAV.map(n => (
        <div key={n.id} className={`fty-nav ${active === n.id ? 'active' : ''}`}>
          <Icon name={n.icon} size={16} />
          <span>{n.label}</span>
          {n.badge && <span className="badge-num">{n.badge}</span>}
        </div>
      ))}

      <div className="fty-nav-section" style={{ marginTop: 12 }}>Environment</div>
      <div className="fty-nav">
        <Icon name="server" size={16} />
        <span>Control plane</span>
        <span className="badge-num" style={{ background: 'hsl(var(--semantic-allow-soft))', color: 'hsl(var(--semantic-allow))' }}>OK</span>
      </div>
      <div className="fty-nav">
        <Icon name="fingerprint" size={16} />
        <span>Signing key</span>
      </div>

      <div className="fty-sidebar-foot">
        <div className="row"><Icon name="radio" size={12} color="hsl(var(--semantic-allow))" /> <span>Serving bundle <span className="mono" style={{ color: 'hsl(var(--foreground))' }}>v14</span></span></div>
        <div className="row"><Icon name="clock" size={12} /> <span>Pushed 4m ago</span></div>
        <div className="row"><Icon name="globe" size={12} /> <span>us-east-1 · 3 regions</span></div>
      </div>
    </aside>
  );
}

function Shell({ active, project = 'support-bot', children, noSidebar = false }) {
  return (
    <div className="fortify">
      <div className="fty-shell">
        <TopBar project={project} />
        <div className="fty-body" style={noSidebar ? { gridTemplateColumns: '1fr' } : undefined}>
          {!noSidebar && <Sidebar active={active} />}
          <main className="fty-main">{children}</main>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { Icon, Shell, TopBar, Sidebar });
