// Filming helper for Playwright browser recordings:
// - a visible cursor (headless Chromium draws none) with a click ripple;
// - smooth zoom done in the page itself (CSS transform on <body>), so text stays sharp;
// - cursor glides with easing; optional "follow" mode keeps the zoomed view on the cursor.
// The cursor lives on <html>, outside the transformed <body>, so it stays at viewport coords.
const { chromium } = require('playwright-core');

const W = 1440, H = 900;

const CURSOR_JS = `(() => {
 const boot = () => {
  const root = document.documentElement;
  if (!root || document.getElementById('__cur')) return;
  const c = document.createElement('div');
  c.id = '__cur';
  c.innerHTML = '<svg width="28" height="32" viewBox="0 0 26 30"><path d="M3 2 L3 24 L9 18.5 L13 27 L17 25.2 L13 16.8 L21 16.5 Z" fill="#fff" stroke="#111" stroke-width="1.6" stroke-linejoin="round"/></svg>';
  Object.assign(c.style, { position: 'fixed', left: '0', top: '0', zIndex: '2147483647', pointerEvents: 'none',
    transform: 'translate(' + (window.__cx ?? -100) + 'px,' + (window.__cy ?? -100) + 'px)',
    filter: 'drop-shadow(0 2px 3px rgba(0,0,0,.5))' });
  const r = document.createElement('div');
  Object.assign(r.style, { position: 'fixed', left: '0', top: '0', width: '44px', height: '44px', marginLeft: '-22px', marginTop: '-22px',
    borderRadius: '50%', border: '3px solid #a594ff', background: 'rgba(165,148,255,.25)', zIndex: '2147483646', pointerEvents: 'none', opacity: '0' });
  root.appendChild(r); root.appendChild(c);
  // backdrop blur samples past the zoomed body's edges and shows mirrored text
  const st = document.createElement('style');
  st.textContent = 'html.__zoomed *, html.__zoomed *::before, html.__zoomed *::after { backdrop-filter: none !important; -webkit-backdrop-filter: none !important; }';
  root.appendChild(st);
  addEventListener('mousemove', (e) => { c.style.transform = 'translate(' + (e.clientX - 3) + 'px,' + (e.clientY - 2) + 'px)'; }, true);
  addEventListener('mousedown', (e) => {
    r.style.left = e.clientX + 'px'; r.style.top = e.clientY + 'px';
    r.animate([{ transform: 'scale(.3)', opacity: 1 }, { transform: 'scale(1.3)', opacity: 0 }], { duration: 500, easing: 'ease-out' });
  }, true);
 };
 // zooming makes the document scrollable; focus or caret moves would scroll it and
 // throw the camera math off, so the window never scrolls while filming.
 addEventListener('scroll', (e) => { if (e.target === document && (scrollX || scrollY)) scrollTo(0, 0); }, true);
 if (document.documentElement) boot(); else document.addEventListener('readystatechange', boot);
 document.addEventListener('DOMContentLoaded', boot);
})();`;

const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class Film {
  constructor(page) {
    this.page = page;
    this.s = 1; this.tx = 0; this.ty = 0;
    this.cu = { x: W * 0.55, y: H * 0.6 }; // cursor in untransformed page coords
    this.following = false;
  }

  static async open(outDir, extra = {}) {
    const browser = await chromium.launch();
    const ctx = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: 1,
      recordVideo: { dir: outDir, size: { width: W, height: H } }, ...extra });
    await ctx.addInitScript(CURSOR_JS);
    const page = await ctx.newPage();
    const f = new Film(page);
    f.browser = browser; f.ctx = ctx;
    return f;
  }

  async close() {
    if (this.drifts) console.error('window scroll drift fixed', this.drifts, 'times');
    const v = this.page.video();
    await this.ctx.close();
    const p = await v.path();
    await this.browser.close();
    return p;
  }

  view(u) { return { x: this.s * u.x + this.tx, y: this.s * u.y + this.ty }; }
  clamp(s, tx, ty) {
    return { s, tx: Math.min(0, Math.max(W - s * W, tx)), ty: Math.min(0, Math.max(H - s * H, ty)) };
  }
  centerOn(s, u) { return this.clamp(s, W / 2 - s * u.x, H / 2 - s * u.y); }

  async apply() {
    const { s, tx, ty } = this;
    const v = this.view(this.cu);
    const sy = await this.page.evaluate(([s, tx, ty]) => {
      const b = document.body; if (!b) return 0;
      const drift = scrollX || scrollY; if (drift) scrollTo(0, 0);
      b.style.transformOrigin = '0 0';
      const zoomed = !(s === 1 && tx === 0 && ty === 0);
      b.style.transform = zoomed ? `translate(${tx}px,${ty}px) scale(${s})` : '';
      document.documentElement.classList.toggle('__zoomed', zoomed);
      return drift;
    }, [s, tx, ty]).catch(() => 0);
    if (sy) this.drifts = (this.drifts || 0) + 1;
    await this.page.mouse.move(v.x, v.y);
  }

  // After a navigation the new document has no transform: start unzoomed, cursor where it was.
  async settle() {
    this.s = 1; this.tx = 0; this.ty = 0;
    await this.apply();
  }

  async animateView(to, ms = 700) {
    const from = { s: this.s, tx: this.tx, ty: this.ty };
    const n = Math.max(1, Math.round(ms / 16));
    for (let i = 1; i <= n; i++) {
      const k = ease(i / n);
      this.s = from.s + (to.s - from.s) * k;
      this.tx = from.tx + (to.tx - from.tx) * k;
      this.ty = from.ty + (to.ty - from.ty) * k;
      await this.apply();
      await sleep(16);
    }
  }

  // Box of a locator in untransformed page coords.
  async ubox(target) {
    const b = typeof target === 'string' ? await this.page.locator(target).first().boundingBox() : await target.boundingBox();
    if (!b) throw new Error('no box for ' + target);
    return { x: (b.x - this.tx) / this.s, y: (b.y - this.ty) / this.s, width: b.width / this.s, height: b.height / this.s };
  }

  // Zoom onto an element. scale: fixed, or fit the box (with pad) up to maxScale.
  async zoomTo(target, { scale, pad = 40, maxScale = 2, ms = 800, anchor } = {}) {
    const b = await this.ubox(target);
    const s = scale ?? Math.min(maxScale, W / (b.width + 2 * pad), H / (b.height + 2 * pad));
    const c = { x: b.x + b.width / 2, y: anchor === 'bottom' ? b.y + b.height - H / (2 * s) + pad / s : b.y + b.height / 2 };
    await this.animateView(this.centerOn(Math.max(1, s), c), ms);
  }

  // Zoom onto a box given in viewport coords (as getBoundingClientRect gives them now).
  async zoomToRect(r, { scale = 1.5, ms = 700, pad = 30 } = {}) {
    const u = { x: (r.x - this.tx) / this.s, y: (r.y - this.ty) / this.s, width: r.width / this.s, height: r.height / this.s };
    const viewH = H / scale;
    // when the box is taller than the view, keep its bottom (newest content) in view
    const cy = u.height + 2 * pad > viewH ? u.y + u.height + pad - viewH / 2 : u.y + u.height / 2;
    await this.animateView(this.centerOn(scale, { x: u.x + u.width / 2, y: cy }), ms);
  }

  // Zoom onto a box in untransformed page coords (as a screenshot at zoom 1 shows it).
  async zoomToBox(b, opts) {
    const r = { x: b.x * this.s + this.tx, y: b.y * this.s + this.ty, width: b.width * this.s, height: b.height * this.s };
    await this.zoomToRect(r, opts);
  }

  // A spot of the page with no selector (a chart bar), in untransformed page coords.
  spot(x, y, width = 1, height = 1) {
    return { boundingBox: async () => ({ x: x * this.s + this.tx, y: y * this.s + this.ty, width: width * this.s, height: height * this.s }) };
  }

  async zoomOut(ms = 800) { this.following = false; await this.animateView({ s: 1, tx: 0, ty: 0 }, ms); }

  // Zoom in around the cursor and keep the view on it while it moves.
  async follow(scale = 1.6, ms = 700) {
    this.following = true;
    await this.animateView(this.centerOn(scale, this.cu), ms);
  }

  async moveTo(target, { ms, dx = 0, dy = 0 } = {}) {
    const b = await this.ubox(target);
    const to = { x: b.x + b.width / 2 + dx, y: b.y + b.height / 2 + dy };
    const from = { ...this.cu };
    const dist = Math.hypot(to.x - from.x, to.y - from.y);
    ms = ms ?? Math.min(1100, 350 + dist * 0.9);
    const n = Math.max(1, Math.round(ms / 16));
    // a slight arc, like a hand
    const nx = -(to.y - from.y) / (dist || 1), ny = (to.x - from.x) / (dist || 1), bow = Math.min(60, dist * 0.08);
    const startView = { s: this.s, tx: this.tx, ty: this.ty };
    for (let i = 1; i <= n; i++) {
      const t = i / n, k = ease(t), arc = Math.sin(Math.PI * t) * bow;
      this.cu = { x: from.x + (to.x - from.x) * k + nx * arc, y: from.y + (to.y - from.y) * k + ny * arc };
      if (this.following) {
        const tgt = this.centerOn(startView.s, this.cu);
        // ease the camera toward the cursor rather than locking it
        this.tx += (tgt.tx - this.tx) * 0.12; this.ty += (tgt.ty - this.ty) * 0.12;
      }
      await this.apply();
      await sleep(16);
    }
    if (this.following) await this.animateView(this.centerOn(this.s, this.cu), 350);
  }

  async hover(target, opts = {}) { await this.moveTo(target, opts); await sleep(opts.hold ?? 700); }

  async click(target, opts = {}) {
    await this.moveTo(target, opts);
    await sleep(opts.before ?? 180);
    await this.page.mouse.down(); await sleep(70); await this.page.mouse.up();
    await sleep(opts.after ?? 250);
  }

  async type(text, delay = 40) { await this.page.keyboard.type(text, { delay }); }
  async pause(ms) { await sleep(ms); }
}

module.exports = { Film, W, H, sleep };
