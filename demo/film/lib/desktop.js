// A mock desktop to open an app from: the cursor clicks its dock icon, the
// window grows out of the icon, then maximizes until it is pixel-identical to
// the app full screen, where the film cuts to the real page.
//
// The desktop is served by Playwright on the app's own origin: Chrome refuses
// to frame a localhost app from any other (local network access checks).
const { sleep } = require('./film');

const icon = (bg, inner) =>
  `<svg viewBox="0 0 56 56" width="56" height="56"><rect width="56" height="56" rx="13" fill="${bg}"/>${inner}</svg>`;
const DOCK = [
  ['files', 'Files', icon('#4C8DF6', '<path d="M13 20a4 4 0 0 1 4-4h8l4 4h10a4 4 0 0 1 4 4v12a4 4 0 0 1-4 4H17a4 4 0 0 1-4-4z" fill="#fff" opacity=".95"/>')],
  ['browser', 'Browser', icon('#fff', '<circle cx="28" cy="28" r="15" fill="none" stroke="#2F6FDB" stroke-width="3"/><path d="M13 28h30M28 13c-6 8-6 22 0 30M28 13c6 8 6 22 0 30" fill="none" stroke="#2F6FDB" stroke-width="2.4"/>')],
  ['terminal', 'Terminal', icon('#1C1F26', '<path d="M16 21l7 6-7 6M27 35h12" fill="none" stroke="#E6E8EB" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>')],
  ['notes', 'Notes', icon('#FFD45C', '<rect x="15" y="14" width="26" height="28" rx="3" fill="#fff"/><path d="M20 22h16M20 28h16M20 34h10" stroke="#C9A227" stroke-width="2.4" stroke-linecap="round"/>')],
];

const pageHtml = (app) => `<!doctype html><html><head><meta charset="utf-8"><style>
* { box-sizing: border-box; margin: 0; }
html, body { height: 100%; overflow: hidden; font: 13px/1.4 -apple-system, system-ui, sans-serif; color: #1c1f26; }
body { background:
  radial-gradient(60% 70% at 18% 25%, #c9c1ff 0%, transparent 60%),
  radial-gradient(55% 60% at 85% 20%, #b9dcff 0%, transparent 60%),
  radial-gradient(70% 70% at 70% 90%, #f3c9e6 0%, transparent 60%),
  linear-gradient(160deg, #e9e6ff, #f4f1fb); }
.bar { position: fixed; inset: 0 0 auto 0; height: 30px; display: flex; align-items: center; gap: 18px; padding: 0 16px;
  background: rgba(255,255,255,.45); backdrop-filter: blur(20px); font-weight: 500; transition: transform .5s ease; z-index: 5; }
.bar .app { font-weight: 700; min-width: 70px; }
.bar .right { margin-left: auto; display: flex; gap: 16px; }
.dock { position: fixed; left: 50%; bottom: 10px; transform: translateX(-50%); display: flex; gap: 10px; padding: 9px 12px;
  border-radius: 22px; background: rgba(255,255,255,.4); border: 1px solid rgba(255,255,255,.6); backdrop-filter: blur(24px);
  box-shadow: 0 10px 30px rgba(40,30,90,.15); transition: transform .5s ease; z-index: 5; }
.dock .it { position: relative; width: 56px; height: 56px; transition: transform .15s ease; }
.dock .it svg { display: block; filter: drop-shadow(0 3px 6px rgba(0,0,0,.18)); border-radius: 13px; }
.dock .it .tip { position: absolute; bottom: 70px; left: 50%; transform: translateX(-50%); white-space: nowrap; padding: 3px 10px;
  border-radius: 7px; background: rgba(30,30,40,.82); color: #fff; font-size: 12px; opacity: 0; transition: opacity .15s; pointer-events: none; }
.dock .it:hover { transform: translateY(-6px) scale(1.08); }
.dock .it:hover .tip { opacity: 1; }
.dock .it .run { position: absolute; bottom: -6px; left: 50%; width: 4px; height: 4px; margin-left: -2px; border-radius: 50%; background: #333; opacity: 0; }
.dock .it.open .run { opacity: .7; }
.dock .it.bounce { animation: bounce .45s ease 2; }
@keyframes bounce { 50% { transform: translateY(-16px); } }
.win { position: fixed; left: 100px; top: 54px; width: 1240px; height: 742px; border-radius: 12px; overflow: hidden; background: #fff;
  box-shadow: 0 30px 80px rgba(30,20,80,.28), 0 0 0 1px rgba(0,0,0,.08); display: none; flex-direction: column; z-index: 3; }
.win .title { height: 40px; flex-shrink: 0; display: flex; align-items: center; padding: 0 14px; background: #f3f4f7; border-bottom: 1px solid #e3e6eb; overflow: hidden; }
.win .dots { display: flex; gap: 8px; }
.win .dots i { width: 12px; height: 12px; border-radius: 50%; background: #d5d9e0; }
.win .addr { margin: 0 auto; transform: translateX(-26px); display: flex; align-items: center; gap: 6px; padding: 5px 14px; border-radius: 8px;
  background: #fff; border: 1px solid #e3e6eb; color: #5b6472; font-size: 12px; min-width: 360px; justify-content: center; }
.win iframe { flex-grow: 1; border: 0; width: 100%; background: #f5f6f8; }
</style></head><body>
<div class="bar"><span class="app" id="appname">Desktop</span><span>File</span><span>Edit</span><span>View</span><span>Window</span>
  <span class="right"><span>Wi-Fi</span><span id="clock"></span></span></div>
<div class="dock">${[...DOCK, ['app', app.name, app.icon]].map(([id, label, svg]) => `<div class="it" id="dock-${id}">${svg}<span class="tip">${label}</span><span class="run"></span></div>`).join('')}</div>
<div class="win" id="win"><div class="title"><span class="dots"><i></i><i></i><i></i></span>
  <span class="addr"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#7a8391" stroke-width="2.4"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>${app.address}</span></div>
  <iframe id="frame"></iframe></div>
<script>
const d = new Date();
document.getElementById('clock').textContent = d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' }) + '  ' +
  d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
</script></body></html>`;

/**
 * Open an app from the dock; ends on the real `app.url`, full screen.
 * app: { url, name, icon (a 56x56 SVG string), address (shown in the title bar) }
 */
async function openFromDesktop(f, app) {
  const { page } = f;
  const desk = new URL('/__desktop', app.url).href;
  await page.route(desk, (r) => r.fulfill({ contentType: 'text/html', body: pageHtml(app) }));
  await page.goto(desk);
  await f.settle();
  await sleep(1400);
  await f.hover('#dock-app', { hold: 700 });
  await f.click('#dock-app', { after: 0 });

  // launch: the dock icon bounces while the window grows out of it
  await page.evaluate(async ([url, name]) => {
    const icon = document.getElementById('dock-app');
    icon.classList.add('bounce', 'open');
    document.getElementById('appname').textContent = name;
    const frame = document.getElementById('frame');
    const loaded = new Promise((r) => (frame.onload = r));
    frame.src = url;
    await loaded;
    const w = document.getElementById('win');
    w.style.display = 'flex';
    const a = icon.getBoundingClientRect(), b = w.getBoundingClientRect();
    const sx = a.width / b.width, sy = a.height / b.height;
    await w.animate([
      { transform: `translate(${a.left - b.left}px, ${a.top - b.top}px) scale(${sx}, ${sy})`, transformOrigin: '0 0', opacity: 0.2 },
      { transform: 'none', transformOrigin: '0 0', opacity: 1 },
    ], { duration: 520, easing: 'cubic-bezier(.2,.8,.2,1)' }).finished;
  }, [app.url, app.name]);
  await sleep(1600);

  // maximize: chrome slides away until the window is exactly the viewport
  await page.evaluate(async () => {
    const w = document.getElementById('win');
    document.querySelector('.bar').style.transform = 'translateY(-100%)';
    document.querySelector('.dock').style.transform = 'translate(-50%, 140%)';
    const t = w.querySelector('.title');
    const opts = { duration: 650, easing: 'cubic-bezier(.4,0,.2,1)', fill: 'forwards' };
    const r = w.getBoundingClientRect();
    await Promise.all([
      w.animate([
        { left: r.left + 'px', top: r.top + 'px', width: r.width + 'px', height: r.height + 'px', borderRadius: '12px' },
        { left: '0px', top: '0px', width: innerWidth + 'px', height: innerHeight + 'px', borderRadius: '0px' },
      ], opts).finished,
      t.animate([{ height: '40px' }, { height: '0px', borderBottomWidth: '0px' }], opts).finished,
    ]);
  });
  await sleep(250);
  // the same page, now for real: the cut is invisible
  await page.goto(app.url);
  await f.settle();
}

module.exports = { openFromDesktop };
