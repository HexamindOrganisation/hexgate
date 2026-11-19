// Signed-in browser state for the hexgate dashboard, made in a context that is
// not recorded, so a dashboard scene starts on the page it films.
const { chromium } = require('playwright-core');
const { env } = require('./env');

async function dashboardState() {
  if (!env.orgId || !env.projectId) throw new Error('FILM_ORG_ID and FILM_PROJECT_ID are not set (run through ./demo/run film)');
  const b = await chromium.launch();
  const c = await b.newContext({ viewport: { width: 1440, height: 900 } });
  const p = await c.newPage();
  await p.goto(env.dash + '/v1/demo-login');
  await p.waitForTimeout(1500);
  const active = JSON.stringify({ state: { activeOrgId: env.orgId, activeProjectId: env.projectId }, version: 0 });
  await p.evaluate((a) => localStorage.setItem('hexgate-active', a), active);
  const storageState = await c.storageState();
  await b.close();
  // Times on screen read as the presenter's local time.
  return { storageState, timezoneId: 'Europe/Paris' };
}

// A tab of the agent hub (scoped to <main>: the sidebar has links with the same names).
const tab = (f, name) => f.page.locator('main').getByText(name, { exact: true }).first();

module.exports = { dashboardState, tab };
