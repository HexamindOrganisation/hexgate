// Records a live session in a terminal app (minmux, Electron) driven by
// Playwright. Used for the scene where Claude Code does the work: nobody can
// script what Claude types, so the session is steered as it runs.
//
//   node lib/terminal.js <out dir> <cwd>
//
// Control it by appending JSON lines to <out dir>/cmd.jsonl:
//   {"type": "claude", "delay": 60}   type text (delay per key, ms)
//   {"key": "Enter"}                  press a key
//   {"wait": 2000}                    pause
//   {"shot": "name"}                  screenshot to <out dir>/name.png (to watch it)
//   {"mark": "prompt sent"}           a timestamped line in log.txt, for the cut
//   {"quit": true}                    close and finish the video
// log.txt has every command with its time since launch; the raw video is in
// <out dir>/video/.
const { _electron } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const [OUT, CWD] = process.argv.slice(2);
if (!OUT || !CWD) {
  console.error('usage: node lib/terminal.js <out dir> <cwd>');
  process.exit(2);
}
const APP = process.env.MINMUX_APP || '/Applications/minmux.app/Contents/MacOS/minmux';
const PROFILE = process.env.MINMUX_PROFILE || 'hexgate';
fs.mkdirSync(OUT, { recursive: true });
const CMD = path.join(OUT, 'cmd.jsonl');
const LOG = path.join(OUT, 'log.txt');
fs.writeFileSync(CMD, '');

// The filmed Claude must be the user's own login, not this session's.
const env = { ...process.env };
for (const k of Object.keys(env)) if (k.startsWith('CLAUDE') || k.startsWith('ANTHROPIC')) delete env[k];

(async () => {
  const t0 = Date.now();
  const log = (m) => fs.appendFileSync(LOG, `${((Date.now() - t0) / 1000).toFixed(1)}s ${m}\n`);
  const app = await _electron.launch({
    executablePath: APP,
    args: [`--profile=${PROFILE}`],
    cwd: CWD,
    env,
    recordVideo: { dir: path.join(OUT, 'video'), size: { width: 1440, height: 900 } },
    timeout: 30000,
  });
  await app.evaluate(({ BrowserWindow }) => {
    const w = BrowserWindow.getAllWindows()[0];
    w.setSize(1440, 900);
    w.center();
  });
  const win = await app.firstWindow();
  log('launched');
  let done = 0;
  for (;;) {
    const lines = fs.readFileSync(CMD, 'utf8').split('\n').filter(Boolean);
    for (; done < lines.length; done++) {
      const c = JSON.parse(lines[done]);
      if (c.type !== undefined) await win.keyboard.type(c.type, { delay: c.delay ?? 25 });
      if (c.key) await win.keyboard.press(c.key);
      if (c.wait) await win.waitForTimeout(c.wait);
      if (c.shot) await win.screenshot({ path: path.join(OUT, `${c.shot}.png`) });
      if (c.mark) log('mark ' + c.mark);
      if (c.quit) {
        log('quit');
        await app.close();
        log('closed');
        return;
      }
      log('done ' + lines[done].slice(0, 80));
    }
    await new Promise((r) => setTimeout(r, 300));
  }
})().catch((e) => {
  fs.appendFileSync(LOG, 'ERR ' + e.stack + '\n');
  process.exit(1);
});
