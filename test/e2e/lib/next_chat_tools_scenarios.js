'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { setTimeout: sleep } = require('node:timers/promises');
const { readViewport, clearAndSettle } = require('./viewport_settle');

const A = 'web-gate-1', B = 'web-gate-survivor', C = 'web-gate-empty', ASSISTANT = 'web-gate-assistant';
const rail = '.chats-button';
const menu = '.popover:not([hidden])';
const card = seat => `[data-pane=${seat}]`;
const title = seat => card(seat) + ' .next-chat-title';
const row = name => `${menu} [data-stream-id="local:${name}"]`;
const action = (name, selector) => `${menu} .next-chat-row:has([data-stream-id="local:${name}"]) ${selector}`;
const editing = menu + ' .next-chat-edit';
const query = selector => `document.querySelector(${JSON.stringify(selector)})`;
const text = selector => `${query(selector)}?.textContent.trim()`;
const shown = selector => `!!${query(selector)}?.getClientRects().length`;
const wait = (ctx, condition, label) => ctx.session.waitFor(condition, { timeoutMs: ctx.timeoutMs, label });

async function command(ctx, data) {
  if (!ctx.runtime.fixtureScratch) throw new Error('Next chat tools need the isolated fixture daemon');
  const dir = ctx.runtime.fixtureScratch;
  const file = path.join(dir, 'next-chat-tools-command.json');
  const nonce = randomUUID();
  fs.writeFileSync(file + '.tmp', JSON.stringify({ ...data, nonce }));
  fs.renameSync(file + '.tmp', file);
  const limit = Date.now() + ctx.timeoutMs;
  while (Date.now() < limit) {
    const done = path.join(dir, 'next-chat-tools-done.json');
    if (fs.existsSync(done)) {
      const result = JSON.parse(fs.readFileSync(done, 'utf8'));
      if (result.nonce === nonce) {
        if (!result.ok) throw new Error(result.error);
        return;
      }
    }
    await sleep(50);
  }
  throw new Error('Next chat tools fixture did not acknowledge command');
}

function calls(ctx) {
  const file = path.join(ctx.runtime.fixtureScratch, 'next-chat-tools-calls.jsonl');
  return fs.existsSync(file) ? fs.readFileSync(file, 'utf8').trim().split('\n').filter(Boolean).map(s => JSON.parse(s)) : [];
}
async function waitCall(ctx, predicate) {
  const limit = Date.now() + ctx.timeoutMs;
  while (Date.now() < limit) {
    const row = calls(ctx).find(predicate);
    if (row) return row;
    await sleep(50);
  }
  throw new Error('Expected Next chat tools daemon request did not arrive');
}

async function enter(ctx, width) {
  await ctx.session.send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: false });
  await ctx.session.send('Page.navigate', { url: ctx.url + 'next/' });
  await wait(ctx, `!!${query(rail)} && [...window.PentacleChatStore.getState().sessions].some(s=>s.stream_id==='local:${A}')`, 'fixture inventory loaded');
}

const workspaceKeys = `key => key.startsWith('pentacle.next.') || key === 'pentacle.workspace.v1'`;
async function saveFixtureBaseline(ctx) {
  const {session:s} = ctx;
  return {
    viewport: await readViewport(s),
    ...await s.eval(`(() => {
      const keys = Object.keys(localStorage).filter(${workspaceKeys}).sort();
      return { storage: Object.fromEntries(keys.map(k=>[k,localStorage.getItem(k)])),
        dpr: devicePixelRatio, reduced: matchMedia('(prefers-reduced-motion: reduce)').matches,
        dark: matchMedia('(prefers-color-scheme: dark)').matches };
    })()`),
  };
}
async function restoreFixtureBaseline(ctx, baseline, restoreNextAfterNavigation) {
  const {session:s} = ctx;
  const snapshot=JSON.stringify(baseline.storage);
  await s.eval(`(() => {
    for (const key of Object.keys(localStorage).filter(${workspaceKeys})) localStorage.removeItem(key);
    for (const [key,value] of Object.entries(${snapshot})) localStorage.setItem(key,value);
    return true;
  })()`);
  await s.send('Emulation.setEmulatedMedia',{media:'',features:[]});
  const scheme = `matchMedia('(prefers-color-scheme: dark)').matches === ${baseline.dark}`;
  const motion = `matchMedia('(prefers-reduced-motion: reduce)').matches === ${baseline.reduced}`;
  if (!await s.eval(`(${scheme}) && (${motion})`)) {
    await s.send('Emulation.setEmulatedMedia',{media:'',features:[
      {name:'prefers-color-scheme',value:baseline.dark?'dark':'light'},
      {name:'prefers-reduced-motion',value:baseline.reduced?'reduce':'no-preference'},
    ]});
  }
  // Let the restored old Web render at natural metrics before judging those
  // metrics. If Chrome's new frame acquired different browser chrome, restore
  // the actual baseline content dimensions rather than passing a drift on.
  await s.send('Emulation.clearDeviceMetricsOverride');
  await s.send('Page.navigate',{url:ctx.url});
  await wait(ctx,"document.readyState === 'complete' && !!document.getElementById('session-list') && !document.querySelector('.next-app')",'old web layout restored');
  try {
    await clearAndSettle(s,baseline.viewport,{timeoutMs:2000});
  } catch (error) {
    if (!String(error.message).startsWith('viewport restore timed out')) throw error;
    await s.send('Emulation.setDeviceMetricsOverride',{
      ...baseline.viewport,deviceScaleFactor:baseline.dpr,mobile:false,
    });
  }
  const nextStorage = JSON.stringify(Object.fromEntries(
    Object.entries(baseline.storage).filter(([key]) => key.startsWith('pentacle.next.'))
  ));
  if (restoreNextAfterNavigation) {
    // Next can write on page exit. Reapply only its keys once the old Web page has settled.
    await s.eval(`(() => {
      const expected = ${nextStorage};
      for (const key of Object.keys(localStorage).filter(k => k.startsWith('pentacle.next.'))) {
        if (!(key in expected)) localStorage.removeItem(key);
      }
      for (const [key, value] of Object.entries(expected)) localStorage.setItem(key, value);
    })()`);
  }
  const {width,height}=baseline.viewport;
  await wait(ctx, `(() => {
    const unmet = [];
    if (innerWidth !== ${width} || innerHeight !== ${height} || devicePixelRatio !== ${baseline.dpr}) unmet.push('viewport');
    if (!(${scheme}) || !(${motion})) unmet.push('media');
    const expected = ${nextStorage};
    const keys = new Set([...Object.keys(expected), ...Object.keys(localStorage).filter(k => k.startsWith('pentacle.next.'))]);
    const different = [...keys].filter(key => localStorage.getItem(key) !== expected[key]).sort();
    if (different.length) unmet.push('Next storage keys: ' + different.join(', '));
    if (unmet.length) throw new Error(unmet.join('; '));
    return true;
  })()`, 'viewport, media and Next storage restored');
}
function isolated(run, { width = 1440, restoreNextAfterNavigation = false } = {}) {
  return async ctx => {
    const baseline = await saveFixtureBaseline(ctx);
    try {
      await command(ctx, { reset: true, settings: { enabled: true } });
      await enter(ctx, width);
      await ctx.session.eval("localStorage.removeItem('pentacle.next.chat.seen'); true");
      await enter(ctx, width);
      await wait(ctx, "localStorage.getItem('pentacle.next.chat.seen') !== null", 'initial seen marks');
      await run(ctx);
    } finally {
      try { await command(ctx, { reset: true }); }
      finally { await restoreFixtureBaseline(ctx, baseline, restoreNextAfterNavigation); }
    }
  };
}

async function select(ctx, name, from = rail) {
  await ctx.session.click(from);
  await wait(ctx, shown(row(name)), 'chat row available');
  await ctx.session.click(row(name));
}
const renameButton = name => query(action(name, '.next-chat-rename'));
const renameReady = (ctx,name) => wait(ctx,`${renameButton(name)}?.disabled===false`,
  `Rename for ${name} enabled after reply and inventory`);
async function rename(ctx, name, value, mode) {
  if (!await ctx.session.eval(`${renameButton(name)}?.disabled===false`))
    throw new Error(`Cannot rename ${name}: Rename is missing or still disabled by an earlier request`);
  await clickAction(ctx, name, '.next-chat-rename');
  await wait(ctx,`!!${query(editing)}`,`inline Rename input opens for ${name}`);
  await ctx.session.eval(`(() => { const el=${query(editing)}; el.value=${JSON.stringify(value)};
    if (${mode === 'blur'}) el.blur();
    else el.dispatchEvent(new KeyboardEvent('keydown',{ key:${JSON.stringify(mode)}, bubbles:true, cancelable:true }));
    return true; })()`);
}
async function clickAction(ctx, name, cls) {
  await ctx.session.eval(`${query(row(name))}.focus(); true`);
  await ctx.session.click(action(name, cls));
}
async function send(ctx, seat, value) {
  const message = query(card(seat) + ' [data-next-message]');
  await ctx.session.eval(`(() => { const el=${message}; el.value=${JSON.stringify(value)};
    el.dispatchEvent(new Event('input',{bubbles:true}));
    el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true})); return true; })()`);
  await waitCall(ctx, r => r.verb === 'send' && r.text === value);
}

const switching = isolated(async ctx => {
  await select(ctx, A);
  await ctx.session.click(title('chat'));
  const position = await ctx.session.eval(`(() => {const menu=${query(menu)}.getBoundingClientRect(), head=${query(title('chat'))}.getBoundingClientRect(); return menu.width===316 && menu.top>=head.bottom && menu.left<=head.left; })()`);
  ctx.report.ok('title menu is beneath the title and 316px wide', position);
  await ctx.session.click(row(B));
  await wait(ctx, `${query('[data-pane=chat] .thread-inner')}.dataset.streamId==='local:${B}'`, 'in-place switch');
  await select(ctx, A);
  await select(ctx, A, title('chat'));
  await wait(ctx, `${query('[data-pane=chat] .thread-inner')}.dataset.streamId==='local:${A}' && ${query('[data-pane=chat2] .thread-inner')}.dataset.streamId==='local:${B}'`, 'two visible cards trade identities');
  await ctx.session.click(title('chat2'));
  await ctx.session.click(menu + ' .next-chat-new');
  ctx.report.ok('new chat stays in the chosen second card', await ctx.session.eval(shown('[data-pane=chat2] .new-chat-host')));
});

const head = isolated(async ctx => {
  await command(ctx, { settings: { rows: {
    [A]: { model: 'claude-opus-5-5', effort: 'high' },
    [B]: { model: 'claude-sonnet-5', effective_model: 'gpt-6-sol', effort: 'medium', effective_effort: 'default' },
  } } });
  await select(ctx, A);
  await wait(ctx, `${text('[data-pane=chat] .next-chat-model')}==='Opus 5.5 · high'`, 'plain model and effort');
  await select(ctx, B, title('chat'));
  await wait(ctx, `${text('[data-pane=chat] .next-chat-model')}==='6 Sol'`, 'effective model and default effort');
});

const renaming = isolated(async ctx => {
  await select(ctx, A);
  await ctx.session.click(rail);
  await rename(ctx, A, 'Rail renamed', 'Enter');
  await wait(ctx, `${text(row(A))}==='Rail renamed' && ${text(title('chat'))}==='Rail renamed'`, 'rail rename visible');
  await waitCall(ctx, r => r.verb === 'rename' && r.session_name === A && r.display_name === 'Rail renamed');
  await ctx.session.click(title('chat'));
  await renameReady(ctx,A);
  await rename(ctx, A, 'Title renamed', 'blur');
  await wait(ctx, `${text(row(A))}==='Title renamed' && ${text(title('chat'))}==='Title renamed'`, 'blur rename visible');
  await waitCall(ctx, r => r.verb === 'rename' && r.session_name === A && r.display_name === 'Title renamed');
  await renameReady(ctx,A);
  await rename(ctx, A, 'Escaped name', 'Escape');
  await ctx.session.click(rail);
  ctx.report.ok('Escape sends nothing and both lists reflect rename', !calls(ctx).some(r => r.display_name === 'Escaped name')
    && await ctx.session.eval(`${text(row(A))}==='Title renamed'`));
  await renameReady(ctx,A);
  await rename(ctx, A, 'Refused name', 'Enter');
  await waitCall(ctx, r => r.verb === 'rename' && r.display_name === 'Refused name');
  await wait(ctx, `${text(row(A))}==='Title renamed' && ${text(title('chat'))}==='Title renamed'`, 'rename refusal restores previous name');
});

const deleting = isolated(async ctx => {
  await command(ctx, { settings: { refuse_close: B, rows: { [C]: { role: 'web-gate-chat-tools-protected' } } } });
  await select(ctx, A); await select(ctx, B);
  await ctx.session.click(rail);
  ctx.report.ok('protected row is listed without mutation actions', await ctx.session.eval(`!!${query(row(C))} && !${query(action(C,'.next-chat-rename'))} && !${query(action(C,'.next-chat-delete'))}`));
  await clickAction(ctx, A, '.next-chat-delete');
  await wait(ctx, `${text(menu + ' .next-chat-question')}==='Delete chat?'`, 'in-row confirmation');
  await ctx.session.click(menu + ' .next-chat-cancel');
  ctx.report.ok('Cancel restores row without close request', !calls(ctx).some(r=>r.verb === 'close') && await ctx.session.eval(shown(row(A))));
  await clickAction(ctx, B, '.next-chat-delete');
  await ctx.session.click(menu + ' .next-chat-confirm');
  await waitCall(ctx, r=>r.verb==='close' && r.session_name===B);
  await wait(ctx, `${shown(row(B))} && ${query(action(B,'.next-chat-delete'))}?.disabled === false && ${shown(card('chat2'))}`, 'refusal leaves the other chat in place');
  await clickAction(ctx, A, '.next-chat-delete');
  await ctx.session.click(menu + ' .next-chat-confirm');
  const close = await waitCall(ctx, r=>r.verb==='close' && r.session_name===A);
  ctx.report.ok('close received both confirmation and force', close.operator_confirm === true && close.force === true);
  await wait(ctx, `!${query(row(A))} && ${query(card('chat'))}.hidden && ${shown(card('chat2'))}`, 'rail and card update before inventory removal');
  await ctx.session.click(title('chat2'));
  ctx.report.ok('deleted chat absent from surviving title list', await ctx.session.eval(`!${query(row(A))}`));
});

const unread = isolated(async ctx => {
  await select(ctx, A);
  await command(ctx, { settings:{ rows:{[B]: {last_kind:'ASSIST',last_text:'New offstage answer'} } } });
  await wait(ctx, `${query(rail)}.getAttribute('aria-label')==='Chats, 1 unread'`, 'one unread on badge and label');
  await ctx.session.click(rail);
  await wait(ctx, shown(action(B, '.next-chat-dot')), 'rail row unread dot');
  await ctx.session.click(title('chat'));
  await wait(ctx, shown(action(B, '.next-chat-dot')), 'title row unread dot');
  await enter(ctx, 1440);
  await wait(ctx, `${query(rail)}.dataset.count==='1'`, 'unread preserved across reload');
  await select(ctx, B);
  await wait(ctx, `!${query(rail)}.dataset.count`, 'opening chat clears unread');
  await ctx.session.click(rail);
  await wait(ctx,`!!${query(row(B))} && !${query(action(B,'.next-chat-dot'))}`,'opened chat loses its dot in the rail list');
  await ctx.session.click(title('chat'));
  await wait(ctx,`!!${query(row(B))} && !${query(action(B,'.next-chat-dot'))}`,'opened chat loses its dot in the title list');
  await ctx.session.click(title('chat'));
  await ctx.session.click('.brand');
  await wait(ctx, `${query('[data-pane=assistant]')}.hidden`, 'Assistant aside');
  await command(ctx, { settings:{ rows:{[B]: {last_kind:'ASSIST',last_text:'New offstage answer'}, [ASSISTANT]: {last_kind:'ASSIST',last_text:'Assistant aside update'} } } });
  await wait(ctx, `${query('.brand')}.classList.contains('assistant-unread')`, 'assistant mark gets unread dot');
  await ctx.session.click('.brand');
  await wait(ctx, `!${query('.brand')}.classList.contains('assistant-unread')`, 'bringing Assistant out clears unread');
}, { restoreNextAfterNavigation: true });

const work = isolated(async ctx => {
  await select(ctx, A);
  const active = { [A]: {working:true,working_label:'7s'}, [ASSISTANT]: {working:true} };
  await command(ctx, { settings:{ rows:active }, elapsed:{[A]:7000,[ASSISTANT]:7000} });
  const timer = '[data-pane=chat] .next-working-time';
  await wait(ctx, shown(timer), 'chat working row visible');
  const initial = await ctx.session.eval(text(timer));
  await wait(ctx, `${text(timer)}!==${JSON.stringify(initial)} && !!${text(timer)}`, 'elapsed clock moves');
  ctx.report.ok('Assistant row and breathing mark, no Stop', await ctx.session.eval(`${shown('[data-pane=assistant] .next-working')} && !${query('[aria-label=Stop]')} && getComputedStyle(${query('.brand .logo')}).animationName==='next-assistant-breathe'`));
  await ctx.session.click(rail);
  await wait(ctx, shown(action(A, '.next-ring')), 'working chat has rail ring');
  await ctx.session.click(title('chat'));
  await wait(ctx, shown(action(A, '.next-ring')), 'working chat has title ring');
  await ctx.session.send('Emulation.setEmulatedMedia', {features:[{name:'prefers-reduced-motion',value:'reduce'}]});
  await wait(ctx, `getComputedStyle(${query('.brand .logo')}).animationName==='none' && getComputedStyle(${query('.brand .logo')}).outlineStyle==='solid'`, 'reduced-motion still ring');
  await command(ctx, { settings:{ rows:{[A]:{working:false},[ASSISTANT]:{working:false}} }, elapsed:{[A]:0,[ASSISTANT]:0} });
  await wait(ctx, `!${shown('[data-pane=chat] .next-working')} && !${query(action(A,'.next-ring'))} && !${query('.brand')}.classList.contains('assistant-working')`, 'rows, ring and mark finish');
});

const sendStatus = isolated(async ctx => {
  const line = status => '[data-pane=chat] .slot-chat-send-status.is-' + status;
  await select(ctx, A);
  const idle = 'Chat tools idle';
  await send(ctx, 'chat', idle);
  await wait(ctx, `${text(line('sending'))}==='Sending' && !!${query(line('sending')+' .stream')}`, 'animated sending line');
  await command(ctx,{result:idle});
  await wait(ctx, shown(line('sent')+' .check'), 'landed send has check');
  for (const [name, allowed] of [[A,true],[B,false]]) {
    await command(ctx,{settings:{rows:{[name]:{working:true,capabilities:{queued_send_now:allowed}}}}});
    if (name !== A) await select(ctx, name, title('chat'));
    const queued = 'Chat tools queued ' + name;
    await send(ctx,'chat',queued);
    await command(ctx,{result:queued});
    await wait(ctx, shown(line('queued')+' .chevron'), 'provider queued chevron');
    // Bind the proof to this exact synthetic message, not a stale idle row
    // or another queued send on the same chat.
    const queuedRow = `[...document.querySelectorAll('[data-pane=chat] article.slot-chat-row.is-user')]
      .find(row => row.querySelector('.slot-chat-user-bubble')?.textContent.trim()===${JSON.stringify(queued)})`;
    await wait(ctx,`!!(${queuedRow})?.querySelector('.slot-chat-send-status.is-queued')`,'selected message has its own queued line');
    const canSend = await ctx.session.eval(`!!(${queuedRow})?.querySelector('.slot-chat-send-now')`);
    ctx.report.ok('selected queued capability reflected for '+name,canSend===allowed);
    if (allowed) {
      const origin = await waitCall(ctx,r=>r.verb==='send' && r.text===queued);
      await ctx.session.eval(`(${queuedRow}).querySelector('.slot-chat-send-now').click(); true`);
      const request = await waitCall(ctx,r=>r.verb==='send.interrupt' && r.session_name===name);
      ctx.report.ok('Send now selects the exact queued origin and optimistic ids',
        !!origin.optimistic_id && request.queued_send_now.original_request_id===origin.request_id &&
        request.queued_send_now.optimistic_id===origin.optimistic_id);
      try {
        await wait(ctx,`!!(${queuedRow}) && !(${queuedRow}).querySelector('.slot-chat-send-status.is-queued')`,
          'correlated completion leaves this message visible without its queued line');
      } catch (error) {
        const dump=await ctx.session.eval(`(() => {
          const id=${JSON.stringify(origin.optimistic_id)}, input=${JSON.stringify(queued)}, state=window.PentacleChatStore.getState();
          return { optimistic:state.optimisticSends?.[id] ?? null,
            events:state.events.filter(e=>e.optimistic_id===id || e.text===input).map(e=>({
              kind:e.kind, stream_id:e.stream_id, timestamp:e.timestamp, daemon_seq:e.daemon_seq,
              optimistic_id:e.optimistic_id, request_id:e.request_id, receipt_state:e.receipt_state,
            })), pendingHtml:(${queuedRow})?.querySelector('.slot-chat-send-status')?.textContent ?? null };
        })()`);
        fs.writeFileSync(path.join(ctx.report.dir,'next-queued-echo-state.json'),JSON.stringify(dump,null,2));
        ctx.report.note('Queued echo did not reconcile; see next-queued-echo-state.json');
        throw error;
      }
    }
  }
  const failed = 'Chat tools failed';
  await send(ctx,'chat',failed);
  await command(ctx,{result:failed});
  await wait(ctx, `${text(line('failed'))}==='Failed to sendRetry' && !!${query(line('failed')+' .slot-chat-send-retry')} && !${query(line('failed')+' .next-send-glyph')}`, 'failed send is unchanged and retryable');
});

const phone = isolated(async ctx => {
  await select(ctx, A);
  await select(ctx, B, title('chat'));
  await command(ctx,{settings:{rows:{[B]:{working:true,working_label:'7s'}}}});
  await wait(ctx, shown('[data-pane=chat] .next-working'), 'working visible on phone');
  await ctx.session.click(title('chat'));
  await ctx.session.eval(`${query(row(B))}.focus(); true`);
  for (const actionClass of ['.next-chat-rename', '.next-chat-delete']) {
    for (const type of ['rawKeyDown', 'keyUp'])
      await ctx.session.send('Input.dispatchKeyEvent', {type, key:'Tab', code:'Tab', windowsVirtualKeyCode:9});
    await wait(ctx, `document.activeElement === ${query(action(B,actionClass))} && ${shown(action(B,actionClass))}`, 'Tab reaches ' + actionClass);
  }
  await ctx.session.click(title('chat'));
  await send(ctx,'chat','Chat tools phone');
  await wait(ctx, shown('[data-pane=chat] .slot-chat-send-status .stream'), 'phone send status visible');
}, { width: 440 });

module.exports = { switching, head, renaming, deleting, unread, work, sendStatus, phone };
