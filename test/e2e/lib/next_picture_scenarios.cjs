'use strict';

const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const F = require('./next_lanes_fixtures');
const H = require('./next_lanes_harness');

const card = '.picture-card';
const shown = card + ':not([hidden])';
const close = card + ' .picture-close';
const full = card + ' .picture-full';
const chat = '.stage > .chat-card:not([hidden])';
const wide = 'fixture-wide.png';
const tall = 'fixture-tall.png';
const firstChat = 'local:web-gate-1';
const secondChat = 'local:web-gate-survivor';
const lane = F.makeOrdinaryLane({ title: 'Fixture picture lane', goalTitle: 'Fixture picture goal', memberCount: 3 });
const thumb = (container, name) => `${container} .slot-chat-media-button[aria-label="${name}"]`;
const wait = (ctx, expr, label) => ctx.session.waitFor(expr, { timeoutMs: ctx.timeoutMs, label });
const visible = selector => `!!document.querySelector(${JSON.stringify(selector + ':not([hidden])')})`;
const overflow = 'document.documentElement.scrollWidth === innerWidth';
const shot = (ctx, name) => ctx.session.screenshot(path.join(ctx.report.dir, name + '.png'));

async function check(ctx, label, expr) {
  const result = await ctx.session.eval(expr);
  ctx.report.ok(label, result === true);
  assert.equal(result, true, label);
}

function seed(ctx) {
  const scratch = ctx.runtime?.fixtureScratch;
  assert.ok(ctx.fixture && ctx.runtime.fixtureDaemonPort && scratch && path.isAbsolute(scratch)
    && path.basename(scratch).startsWith('pentacle-web-gate-'), 'pictures require a disposable web-gate fixture');
  execFileSync(ctx.python, [path.join(__dirname, 'seed_web_gate.py'), '--db',
    path.join(scratch, 'sessions.db'), '--blob-root', path.join(scratch, 'blobs'), '--next-pictures']);
}

async function openChat(ctx, id) {
  await ctx.session.click('.rail [aria-label="Chats"]');
  await ctx.session.click(`.popover [data-stream-id="${id}"]`);
  await wait(ctx, `document.querySelector(${JSON.stringify(chat + ' .thread-inner')})?.dataset.streamId === ${JSON.stringify(id)}`, 'source chat opened');
}

async function image(ctx, container, name, width, height) {
  const expected = thumb(container, name);
  await wait(ctx, `!!document.querySelector(${JSON.stringify(expected)})`, 'seeded image thumbnail');
  await ctx.session.click(expected);
  // Keep this immediate assertion first: on base the delegated thumbnail
  // click has no action, even though the transcript renders a button.
  await check(ctx, 'ordinary thumbnail opens the picture card', `!!document.querySelector(${JSON.stringify(shown)})`);
  await wait(ctx, `document.querySelector('.picture-image')?.naturalWidth === ${width}`, 'decoded fixture image');
  await check(ctx, 'named picture is scaled to fit and thumbnail is marked', `(() => {
    const img = document.querySelector('.picture-image');
    const well = document.querySelector('.picture-well');
    const bounds = img.getBoundingClientRect();
    const card = well.getBoundingClientRect();
    return img.naturalHeight === ${height} && getComputedStyle(img).objectFit === 'contain'
      && bounds.width === card.width && bounds.height === card.height
      && document.querySelector('.picture-title').textContent === ${JSON.stringify(name)}
      && document.querySelector(${JSON.stringify(expected)}).getAttribute('aria-pressed') === 'true';
  })()`);
}

function isolated(run, { seedPictures = true } = {}) {
  return async ctx => {
    if (seedPictures) seed(ctx);
    let hook;
    try {
      hook = await H.enterNext(ctx);
      await H.installLaneBridge(ctx, { frame: F.inventoryFrame([lane]), versions: H.laneDetailVersions([lane]) });
      await run(ctx);
    } finally {
      await H.cleanupHook(ctx, hook);
      await ctx.session.send('Emulation.clearDeviceMetricsOverride');
      await ctx.session.send('Page.navigate', { url: ctx.url });
      await wait(ctx, '!!document.getElementById("session-list")', 'restore current web');
    }
  };
}

const desktop = isolated(async ctx => {
  const { session } = ctx;
  // The fixture contains both ordinary attachment messages and two names
  // recognized by the real lane-link renderer in the same assistant reply.
  await wait(ctx, `!!document.querySelector(${JSON.stringify(thumb('.pane', wide))})`, 'assistant picture fixture');
  await image(ctx, '.pane', wide, 420, 150);
  await check(ctx, 'Bart and the picture have even widths, and Design is not lit',
    `(() => { const pane=document.querySelector('.pane'), picture=document.querySelector('${card}');
      return !pane.hidden && Math.abs(pane.clientWidth-picture.clientWidth)<3 &&
      !document.querySelector('.rail [aria-label="Dashboards"]').classList.contains('on')
      && document.querySelectorAll('.rail .rail-button').length === 7; })()`);
  await shot(ctx, 'next-picture-wide');
  await session.click(thumb('.pane', wide));
  await check(ctx, 'clicking the open thumbnail closes and unmarks it', `!document.querySelector('${shown}') &&
    document.querySelector(${JSON.stringify(thumb('.pane', wide))}).getAttribute('aria-pressed') === 'false'`);
  await image(ctx, '.pane', wide, 420, 150);
  await image(ctx, '.pane', tall, 150, 420);
  await check(ctx, 'swapping leaves only the second thumbnail marked',
    `document.querySelectorAll('.pane [data-next-picture][aria-pressed="true"]').length === 1`);
  await session.click(full);
  await check(ctx, 'desktop picture full screen hides the chats and lanes', `!document.querySelector('.pane:not([hidden])') &&
    !document.querySelector('.lanes-card:not([hidden])') && document.querySelector('${full}').ariaLabel === 'Leave full screen'`);
  await session.click(full);
  await check(ctx, 'the same control restores the previous split', visible('.pane'));
  await session.click(full);
  await session.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape' });
  await check(ctx, 'Escape also restores the previous split', visible('.pane'));
  await session.click(close);
  await openChat(ctx, firstChat);
  await session.click(chat + ' .fullscreen');
  await image(ctx, chat, tall, 150, 420);
  await check(ctx, 'opening from a solo chat brings it beside the picture instead of Bart',
    `${visible(chat)} && !document.querySelector('.pane:not([hidden])') &&
      Math.abs(document.querySelector(${JSON.stringify(chat)}).clientWidth-document.querySelector('${card}').clientWidth)<3`);
  await shot(ctx, 'next-picture-tall');
  await session.click(close);
  await check(ctx, 'closing restores the full-screen chat', `${visible(chat)} && !document.querySelector('.pane:not([hidden])') &&
    !document.querySelector('.lanes-card:not([hidden])')`);
  await image(ctx, chat, wide, 420, 150);
  await session.click('.rail .brand');
  await check(ctx, 'dim logo brings Bart beside the picture', `${visible('.pane')} && !${visible(chat)} && !!document.querySelector('${shown}')`);
  await openChat(ctx, secondChat);
  await check(ctx, 'rail chat ends picture arrangement without keeping solo', `${visible('.pane')} && ${visible(chat)} && !document.querySelector('${shown}')`);
  const linkRows = '.pane a.slot-chat-md-link';
  await check(ctx, 'ordinary outside and /designs/ links use native new tabs, not Design chips', `(() => {
    const links = [...document.querySelectorAll(${JSON.stringify(linkRows)})];
    return ['https://example.invalid/picture', '/designs/'].every(address =>
      links.some(a => a.getAttribute('href') === address && a.target === '_blank' && a.rel.includes('noopener') && !a.dataset.nextDesignLink));
  })()`);
  await session.click('.pane .next-lane-name-link:not([data-goal-id])');
  await wait(ctx, "document.querySelector('.next-lanes-scroll')?.dataset.level === 'lane'", 'text lane link opens the lane');
  await session.click('.pane .next-lane-name-link[data-goal-id]');
  await wait(ctx, "document.querySelector('.next-lanes-scroll')?.dataset.level === 'goal'", 'text goal link opens the goal');
});

const narrow = isolated(async ctx => {
  const { session } = ctx;
  for (const width of [1010, 702, 440]) {
    await session.send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: false });
    await wait(ctx, `innerWidth === ${width}`, 'compact viewport applied');
    await openChat(ctx, firstChat);
    await image(ctx, chat, tall, 150, 420);
    await check(ctx, `picture fills the stage with no horizontal overflow at ${width}`, `${overflow} &&
      !${visible(chat)} && !${visible('.pane')} && !${visible('.lanes-card')} &&
      document.querySelector('${full}').hidden === ${width < 600}`);
    await shot(ctx, 'next-picture-' + width);
    await session.click(close);
    await check(ctx, `the source chat comes back at ${width}`, `${visible(chat)} && !document.querySelector('${shown}') &&
      (${visible('.pane')}) === ${width >= 600} && ${overflow}`);
  }
});

const themes = isolated(async ctx => {
  for (const theme of ['dark', 'light', 'cosmic']) {
    await ctx.session.click('.rail [aria-label="Settings"]');
    await ctx.session.click(`.theme-choice[aria-label="${theme}"]`);
    await ctx.session.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape' });
    await check(ctx, `${theme} theme has lanes beside Bart`, `document.documentElement.dataset.theme === '${theme}' &&
      ${visible('.pane')} && ${visible('.lanes-card')} && !document.querySelector('${shown}')`);
    await shot(ctx, 'next-themes-composed-' + theme);
  }
  await ctx.session.eval("localStorage.setItem('pentacle.next.theme','dark'); true");
}, { seedPictures: false });

module.exports = [
  ['next-picture', desktop],
  ['next-picture-narrow', narrow],
  ['next-themes-composed', themes],
];
