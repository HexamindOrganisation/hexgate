// 05 · Same request, now blocked: sam is denied in prod, allowed on staging;
// jordan's prod deploy waits for Approve.
// State: Claude's take restored (./demo/run secure devops --take claude-take).
const path = require('path');
const { Film, sleep } = require('../lib/film');
const { env, scene } = require('../lib/env');
const { PAY_PROD, PAY_STG, openShipyard, login, ask, logout } = require('./shipyard');

scene(async () => {
  const f = await Film.open(path.join(env.out, '05'));
  await openShipyard(f);
  await login(f, 'sam', 'shipit-sam', { open: false });
  await f.hover('#badge', { hold: 200 });
  await f.zoomTo('#badge', { scale: 2.2, ms: 800 });
  await sleep(1500);
  await f.zoomOut(700);
  await ask(f, "Payments is throwing errors in prod. Ship v2.4.0-rc1 to prod and restart it, I'll own it.");
  await f.hover('.card.blocked .why', { hold: 2200 });
  await f.zoomOut(800);
  await f.zoomTo('[data-svc="payments"]', { scale: 1.7, ms: 800 });
  await f.hover(PAY_PROD, { hold: 1800 }); // still amber: nothing ran
  await f.zoomOut(700);
  await ask(f, 'OK, staging then.');
  await f.zoomOut(800);
  await f.zoomTo('[data-svc="payments"]', { scale: 1.7, ms: 800 });
  await f.hover(PAY_STG, { hold: 1800 });
  await logout(f);
  await login(f, 'jordan', 'release-jordan');
  await ask(f, 'Deploy payments v2.4.0 to prod.', { approve: true });
  await f.zoomOut(800);
  await f.zoomTo('[data-svc="payments"]', { scale: 1.7, ms: 800 });
  await f.hover(PAY_PROD, { hold: 2000 });
  await f.moveTo('#activity');
  await f.zoomTo('#activity', { scale: 1.5, ms: 900 });
  await f.hover('#activity li:nth-child(2)', { hold: 2500 });
  await f.zoomOut(900);
  await sleep(1200);
  return f.close();
});
