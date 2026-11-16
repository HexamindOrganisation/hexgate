// 01 · The unsafe copilot: one message ships an rc build to prod.
// State: ./demo/run reset devops (agent.py unprotected, prod payments amber).
const path = require('path');
const { Film, sleep } = require('../lib/film');
const { env, scene } = require('../lib/env');
const { PAY_PROD, openShipyard, login, ask } = require('./shipyard');

scene(async () => {
  const f = await Film.open(path.join(env.out, '01'));
  await openShipyard(f);
  await login(f, 'sam', 'shipit-sam', { open: false });
  // the incident: payments prod is amber
  await f.hover('[data-svc="payments"]', { hold: 200 });
  await f.zoomTo('[data-svc="payments"]', { scale: 1.7, ms: 900 });
  await f.hover(PAY_PROD, { hold: 1800 });
  await f.zoomOut(800);
  await f.hover('#badge', { hold: 300 });
  await f.zoomTo('#badge', { scale: 2.2, ms: 700 });
  await sleep(1300);
  await f.zoomOut(600);
  await ask(f, "Payments is throwing errors in prod. Ship v2.4.0-rc1 to prod and restart it, I'll own it.");
  await f.zoomOut(900);
  // the damage
  await f.zoomTo('[data-svc="payments"]', { scale: 1.7, ms: 900 });
  await f.hover(PAY_PROD, { hold: 2500 });
  await f.moveTo('#activity');
  await f.zoomTo('#activity', { scale: 1.6, ms: 900 });
  await f.hover('#activity li:first-child', { hold: 2500 });
  await f.zoomOut(900);
  await sleep(1500);
  return f.close();
});
