// Shipyard moves shared by the app scenes: open it, sign in, ask the copilot.
const fs = require('fs');
const path = require('path');
const { sleep } = require('../lib/film');
const { openFromDesktop } = require('../lib/desktop');
const { env } = require('../lib/env');

const APP = env.app;
const LOGO = fs.readFileSync(path.resolve(__dirname, '../../apps/devops/ui/public/logo.svg'), 'utf8');
// The latest exchange in the copilot thread: last user message down to the newest line.
async function lastExchange(page) {
  return page.evaluate(() => {
    const t = document.querySelector('#thread');
    const users = t.querySelectorAll('.msg-user');
    const from = users[users.length - 1] || t.firstElementChild;
    let x1 = 1e9, y1 = 1e9, x2 = -1e9, y2 = -1e9;
    const add = (el) => { const r = el.getBoundingClientRect(); if (!r.width || !r.height) return;
      x1 = Math.min(x1, r.left); y1 = Math.min(y1, r.top); x2 = Math.max(x2, r.right); y2 = Math.max(y2, r.bottom); };
    add(from);
    let el = from.nextElementSibling;
    while (el) { el.querySelectorAll('*').forEach(add); add(el); el = el.nextElementSibling; }
    return { x: x1, y: y1, width: x2 - x1, height: y2 - y1 };
  });
}
const followThread = async (f, ms = 600) => f.zoomToRect(await lastExchange(f.page), { scale: 1.6, ms });

// The payments cards, by environment.
const PAY_PROD = '[data-svc="payments"] .cell:nth-child(3)';

// Open Shipyard from the mock desktop; ends on its sign-in page.
const openShipyard = (f) =>
  openFromDesktop(f, {
    url: APP + '/login',
    name: 'Shipyard',
    address: 'shipyard.acme.internal',
    icon: LOGO.replace('width="28" height="28"', 'width="56" height="56"'),
  });

// `open: false` when the page is already on /login (opened from the desktop).
async function login(f, user, pw, { open = true } = {}) {
  const { page } = f;
  if (open) {
    await page.goto(APP + '/login');
    await f.settle();
  }
  await sleep(1200);
  await f.zoomTo('form', { maxScale: 1.6, pad: 60, ms: 900 });
  await f.click('#user'); await f.type(user, 90);
  await f.click('#pw'); await f.type(pw, 55);
  await sleep(300);
  await f.click('#go', { after: 0 });
  await page.waitForURL(APP + '/');
  await f.settle();
  await sleep(1500);
}

// Type into the copilot with the camera on the composer, send, then ride the
// thread while it streams; ends unzoomed.
async function ask(f, text, { approve = false } = {}) {
  const { page } = f;
  await f.click('#ask');
  await f.zoomTo('#composer', { scale: 1.9, ms: 800 });
  await f.type(text, 38);
  await sleep(600);
  await f.click('#send');
  await sleep(300);
  await followThread(f, 900);
  const done = () => page.evaluate(() => !document.querySelector('#send').disabled);
  let approved = false;
  for (let i = 0; i < 150; i++) {
    await sleep(700);
    if (approve && !approved) {
      const btn = page.locator('.actions button', { hasText: 'Approve' }).last();
      if (await btn.isVisible().catch(() => false)) {
        await f.zoomTo(page.locator('.actions').last(), { scale: 1.8, ms: 700 });
        await sleep(1800);
        await f.click(btn, { before: 500 });
        approved = true;
        await sleep(600);
        await followThread(f, 700);
        continue;
      }
    }
    if (i > 2 && (await done())) break;
    await followThread(f, 500);
  }
  if (!(await done())) throw new Error(`the copilot was still busy after 105 s: ${text}`);
  await sleep(2500); // read the answer
}

async function logout(f) {
  await f.zoomOut(700);
  await f.hover('#logout', { hold: 500 });
  await f.click('#logout', { after: 0 });
  await f.page.waitForURL(/login/);
  await f.settle();
}

const PAY_STG = '[data-svc="payments"] .cell:nth-child(2)';

module.exports = { APP, PAY_PROD, PAY_STG, openShipyard, login, ask, logout };
