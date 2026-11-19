// 04 · Edit the policy: in the dashboard's policy editor, test a call (allowed),
// lower the scale cap from 20 to 10, test the unsaved draft (denied), save.
// State: Claude's take restored. The save is real: ./demo/run film restores the
// policy afterwards (FILM_NO_SAVE=1 films it without saving).
const path = require('path');
const { Film, sleep } = require('../lib/film');
const { env, scene } = require('../lib/env');
const { dashboardState } = require('../lib/dashboard');

const SAVE = !process.env.FILM_NO_SAVE;

// A box around a substring of the CodeMirror line that contains `lineText`.
const textBox = (page, lineText, sub) => ({
  boundingBox: () => page.evaluate(([lt, s]) => {
    const line = [...document.querySelectorAll('.cm-line')].find((l) => l.textContent.includes(lt));
    const w = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
    let acc = '', n;
    const nodes = [];
    while ((n = w.nextNode())) { nodes.push([n, acc.length]); acc += n.data; }
    const at = acc.indexOf(s, acc.indexOf(lt));
    const r = document.createRange();
    const pos = (i) => { for (let k = nodes.length - 1; k >= 0; k--) if (nodes[k][1] <= i) return [nodes[k][0], i - nodes[k][1]]; };
    r.setStart(...pos(at)); r.setEnd(...pos(at + s.length));
    const b = r.getBoundingClientRect();
    return { x: b.x, y: b.y, width: b.width, height: b.height };
  }, [lineText, sub]),
});

scene(async () => {
  const f = await Film.open(path.join(env.out, '04'), await dashboardState());
  const pg = f.page, main = pg.locator('main');
  await pg.goto(env.dash + '/agents/ops_copilot'); await f.settle(); await sleep(1200);
  await f.click(pg.getByRole('link', { name: 'Policies' }).first(), { after: 0 });
  await sleep(2500); await f.settle();

  // the boundary, in the YAML
  await f.zoomToRect({ x: 495, y: 225, width: 560, height: 250 }, { scale: 1.7, ms: 900 });
  await f.hover(textBox(pg, 'args.replicas', 'args.replicas'), { hold: 1600 });
  await f.zoomOut(700);

  // inspect ops_copilot: the role x tool grid
  const agentSel = main.locator('select').first();
  await f.hover(agentSel, { hold: 300 });
  await agentSel.selectOption('ops_copilot'); await sleep(1500);
  await f.zoomToRect({ x: 1048, y: 55, width: 380, height: 330 }, { scale: 1.8, ms: 800 });
  await sleep(2200);
  await f.zoomOut(600);

  // Test: release scales payments in prod to 15 replicas
  await f.click(main.getByText('Test', { exact: true }).first());
  await sleep(600);
  await f.zoomToRect({ x: 1048, y: 60, width: 380, height: 420 }, { scale: 1.75, ms: 800 });
  const roleSel = main.locator('select').first();
  await f.hover(roleSel, { hold: 200 }); await roleSel.selectOption('release'); await sleep(500);
  await f.click(main.locator('input[placeholder="*"]'));
  await pg.keyboard.press('ControlOrMeta+A'); await f.type('ops_copilot', 45);
  await f.click(main.locator('.cm-content').last());
  await pg.keyboard.press('ControlOrMeta+A'); await pg.keyboard.press('Backspace');
  // typed line by line; closeBrackets supplies (and steps over) the closers
  await f.type('{', 60); await pg.keyboard.press('Enter');
  await f.type('"tool": "scale_service",', 30); await pg.keyboard.press('Enter');
  await f.type('"args": {"env": "prod", "replicas": 15}', 30);
  await sleep(400);
  await f.zoomOut(600);
  const check = main.getByRole('button', { name: /Check/ });
  await f.click(check);
  await sleep(1200);
  await f.zoomToRect({ x: 1048, y: 600, width: 380, height: 300 }, { scale: 1.8, ms: 800 });
  await sleep(2000);
  await f.zoomOut(600);

  // tighten the cap: 20 -> 10
  await f.zoomToRect({ x: 495, y: 40, width: 560, height: 440 }, { scale: 1.6, ms: 900 });
  await f.click(textBox(pg, 'args.replicas', '20'), { dx: 9 });
  await pg.keyboard.press('Backspace'); await sleep(120); await pg.keyboard.press('Backspace'); await sleep(200);
  await f.type('10', 160);
  await sleep(500);
  // and its comment
  await pg.keyboard.press('End'); await sleep(300);
  for (let i = 0; i < ' replicas'.length; i++) { await pg.keyboard.press('ArrowLeft'); await sleep(25); }
  await pg.keyboard.press('Backspace'); await pg.keyboard.press('Backspace'); await f.type('10', 160);
  await sleep(900);
  await pg.keyboard.press('Home');
  await pg.evaluate(() => { const sc = document.querySelector('.cm-scroller'); if (sc) sc.scrollLeft = 0; });
  await f.hover(main.getByText('Unsaved changes'), { hold: 1400 });
  await f.zoomOut(700);

  // the same call against the unsaved draft: denied now
  await f.click(check);
  await sleep(1300);
  await f.zoomToRect({ x: 1048, y: 600, width: 380, height: 300 }, { scale: 1.8, ms: 800 });
  await sleep(3000);
  await f.zoomOut(600);

  // save
  const save = main.getByRole('button', { name: 'Save' });
  await f.moveTo(save); await f.follow(1.8);
  if (SAVE) await f.click(save); else await sleep(300);
  await sleep(1800);
  await f.zoomOut(700);
  await sleep(1500);
  return f.close();
});
