// Renders each scene's Hexgate title card (title-card.html) to a short clip,
// frame by frame: window.__render(t), a screenshot, piped to ffmpeg.
//
//   node lib/cards.js <cut.json> <clips dir> [scene id ...]
//
// Writes <clips dir>/cards/<id>.mp4 for every scene with a "card" in cut.json.
// render.py lays the card over the scene's first seconds, then dissolves it.
// The fonts (Inter, JetBrains Mono) load from Google Fonts: rendering needs
// the network, and fails rather than fall back to other fonts.
const { chromium } = require('playwright-core');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const W = 1440, H = 900, FPS = 25;
const SECONDS = 4; // render.py shows it 3.3 s, then dissolves it over 0.6 s

async function renderCard(page, card, out) {
  await page.addInitScript((c) => { window.CARD = c; }, card);
  await page.goto('file://' + path.join(__dirname, 'title-card.html'));
  await page.evaluate(() => document.fonts.ready);
  const fonts = await page.evaluate(() => ['Inter', 'JetBrains Mono'].every((f) => document.fonts.check(`20px "${f}"`)));
  if (!fonts) throw new Error('Inter or JetBrains Mono did not load (no network?)');
  const ff = spawn('ffmpeg', ['-loglevel', 'error', '-y', '-f', 'image2pipe', '-framerate', String(FPS), '-i', '-',
    '-c:v', 'libx264', '-crf', '14', '-pix_fmt', 'yuv420p', out], { stdio: ['pipe', 'inherit', 'inherit'] });
  const done = new Promise((resolve, reject) => ff.on('close', (code) => (code === 0 ? resolve() : reject(new Error(`ffmpeg ${code}`)))));
  for (let i = 0; i < SECONDS * FPS; i++) {
    await page.evaluate((t) => window.__render(t), i / FPS);
    const png = await page.screenshot({ type: 'png' });
    if (!ff.stdin.write(png)) await new Promise((r) => ff.stdin.once('drain', r));
  }
  ff.stdin.end();
  await done;
}

(async () => {
  const [cutFile, clips, ...only] = process.argv.slice(2);
  if (!cutFile || !clips) {
    console.error('usage: node lib/cards.js <cut.json> <clips dir> [scene id ...]');
    process.exit(2);
  }
  const scenes = JSON.parse(fs.readFileSync(cutFile, 'utf8')).scenes
    .filter((s) => s.card && (!only.length || only.includes(s.id)));
  fs.mkdirSync(path.join(clips, 'cards'), { recursive: true });
  const browser = await chromium.launch();
  try {
    for (const s of scenes) {
      const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: 1 });
      const out = path.join(clips, 'cards', `${s.id}.mp4`);
      await renderCard(page, { part: s.id, ...s.card }, out);
      await page.close();
      console.log(out);
    }
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
