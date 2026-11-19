// 03 · The agent on hexgate: the registered copilot, its graph per role,
// its tools, and the policy as a role x tool grid.
// State: Claude's take restored.
const path = require('path');
const { Film, sleep } = require('../lib/film');
const { env, scene } = require('../lib/env');
const { dashboardState, tab } = require('../lib/dashboard');

scene(async () => {
  const f = await Film.open(path.join(env.out, '03'), await dashboardState());
  await f.page.goto(env.dash + '/agents'); await f.settle(); await sleep(2000);
  await f.click(f.page.locator('main').getByText('ops_copilot').first(), { after: 0 });
  await sleep(2500); await f.settle();
  await f.hover(f.page.locator('main').getByText('Resources').first(), { hold: 300 });
  await f.click(f.page.getByRole('button', { name: 'Expand' }));
  await sleep(1500);
  await f.zoomTo('[role="dialog"]', { maxScale: 1.25, pad: 10, ms: 800 });
  const sel = f.page.locator('[role="dialog"] select').first();
  await f.hover(sel, { hold: 400 });
  for (const r of ['engineer', 'release']) { await sel.selectOption(r); await sleep(3200); }
  await f.hover(f.page.locator('[role="dialog"]').getByText('deploy_version').first(), { hold: 1800 });
  await f.hover(f.page.locator('[role="dialog"]').getByText(/delete_environ/).first(), { hold: 1800 });
  await f.zoomOut(700);
  await f.page.keyboard.press('Escape'); await sleep(1000);
  await f.moveTo(tab(f, 'Overview'));
  await f.follow(1.7);
  await f.click(f.page.locator('main').getByText(/^Resources/).first());
  await sleep(1200);
  await f.zoomOut(800);
  await sleep(2000);
  await f.click(tab(f, 'Policies'));
  await sleep(1500);
  await f.zoomTo('main table', { maxScale: 1.45, pad: 20, ms: 900 });
  const row = (t) => f.page.locator('main table tr', { hasText: t });
  await f.hover(row('deploy_version').locator('td').nth(1), { hold: 1800 });
  await f.hover(row('deploy_version').locator('td').nth(2), { hold: 1800 });
  await f.hover(row('scale_service').locator('td').nth(2), { hold: 1500 });
  await f.hover(row('delete_environment').locator('th'), { hold: 1500 });
  await f.zoomOut(900); await sleep(1200);

  return f.close();
});
