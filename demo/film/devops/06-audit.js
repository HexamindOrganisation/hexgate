// 06 · The audit trail: the stats of scene 05's decisions, the decision log,
// and jordan's approval in detail.
// State: right after 05 (its decisions are what this shows).
const path = require('path');
const { Film, sleep } = require('../lib/film');
const { env, scene } = require('../lib/env');
const { dashboardState, tab } = require('../lib/dashboard');

scene(async () => {
  const f = await Film.open(path.join(env.out, '06'), await dashboardState());
  await f.page.goto(env.dash + '/agents/ops_copilot'); await f.settle(); await sleep(2500);
  const main = f.page.locator('main');
  // Stats: the KPI cards, the decisions per hour, which tools and roles got denied
  await f.click(tab(f, 'Stats'));
  await sleep(1800);
  await f.zoomToBox({ x: 360, y: 278, width: 960, height: 156 }, { scale: 1.45, ms: 800 });
  for (const kpi of ['Decisions', 'Denied', 'Needs approval', 'Allowed']) {
    await f.hover(main.getByText(kpi, { exact: true }).first(), { hold: 900, dy: 30 });
  }
  await f.zoomOut(600);
  await f.zoomToBox({ x: 360, y: 450, width: 594, height: 330 }, { scale: 1.5, ms: 800 });
  await f.hover(f.spot(892, 560, 18, 160), { hold: 1600 }); // this hour's bar: allow, approval, deny
  await f.zoomToBox({ x: 970, y: 450, width: 350, height: 330 }, { scale: 1.7, ms: 800 });
  await f.hover(main.getByText('deploy_version').first(), { hold: 1400 });
  await f.click(main.getByText('Caller roles', { exact: true }).first());
  await sleep(1600);
  await f.zoomOut(700);
  await f.click(tab(f, 'Decision log'));
  await sleep(1500);
  await f.zoomTo(f.page.locator('main div.rounded-xl', { hasText: 'Export JSONL' }).last(), { maxScale: 1.45, pad: 12, ms: 900 });
  const rows = f.page.locator('main button', { hasText: 'ops_copilot' });
  const n = await rows.count();
  for (let i = n - 1; i >= 0; i--) await f.hover(rows.nth(i), { hold: 700, dx: -200 });
  const appr = rows.filter({ hasText: 'approval' }).first();
  await f.click(appr, { dx: -200 });
  await sleep(1200);
  await f.zoomOut(700);
  await f.moveTo(f.page.getByText('granted by').first());
  await f.zoomToRect({ x: 1150, y: 60, width: 290, height: 560 }, { scale: 1.7, ms: 900 });
  await sleep(4500);
  await f.zoomOut(900);
  await sleep(1500);
  return f.close();
});
