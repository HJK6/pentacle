'use strict';

const { fixtureRequest } = require('./closed_chat_scenario');
const F = require('./next_lanes_fixtures');
const H = require('./next_lanes_harness');

const json = value => JSON.stringify(value);
const sel = value => json(value);

function check(ctx, name, condition, detail = {}) {
  const passed = condition === true;
  ctx.report.ok(name, passed, detail);
  if (!passed) throw new Error(`assertion failed: ${name}`);
}

async function wait(ctx, expression, label) {
  await ctx.session.waitFor(expression, { timeoutMs: ctx.timeoutMs, label });
}

async function realPointerClick(ctx, selector) {
  const point = await ctx.session.eval(`(() => {
    const el = document.querySelector(${sel(selector)});
    if (!el) return null;
    el.scrollIntoView({ block: 'center' });
    const rect = el.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, width: rect.width, height: rect.height };
  })()`);
  if (!point || point.width <= 0 || point.height <= 0) throw new Error(`pointer target is not visible: ${selector}`);
  await ctx.session.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: point.x, y: point.y });
  await ctx.session.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: point.x, y: point.y,
    button: 'left', buttons: 1, clickCount: 1 });
  await ctx.session.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: point.x, y: point.y,
    button: 'left', buttons: 0, clickCount: 1 });
}

async function laneCallEvidence(ctx, laneId) {
  return ctx.session.eval(`window.__nextLanes.calls.filter(call=>call.laneId===${sel(laneId)}).map(call=>({
    callId:call.callId,
    membersLimit:call.options.membersLimit==null?null:Number(call.options.membersLimit),
    goalId:call.options.goalId||null,
    revision:call.revision,
    status:call.status,
    answeredRevision:call.answeredRevision||null,
  }))`);
}

async function setInventory(ctx, frame) {
  await ctx.session.eval(`window.__nextLanesSetInventory(${json(frame)})`);
}

async function install(ctx, hooks, width, lanes, frame, options = {}) {
  const hook = await H.enterNext(ctx, width);
  hooks.push(hook);
  await H.installLaneBridge(ctx, {
    frame,
    versions: options.versions || H.laneDetailVersions(lanes),
    failures: options.failures || [],
    holdRules: options.holdRules || [],
  });
  return hook;
}

async function openList(ctx, count) {
  await H.openQuestionList(ctx);
  await wait(ctx, `document.querySelectorAll('.next-question-list-item').length === ${count}`, `question list with ${count} entries`);
}

function rowSelector(laneId) {
  return `.next-lanes-row-open[data-lane-id="${laneId}"]`;
}

async function rowState(ctx, laneId) {
  return ctx.session.eval(`(() => {
    const button = document.querySelector(${sel(rowSelector(laneId))});
    const row = button?.closest('.next-lanes-row');
    return row ? {text:row.textContent, marks:row.querySelectorAll('.next-lanes-question-mark').length,
      attention:[...row.querySelectorAll('.next-lanes-attention')].map(x=>({text:x.textContent.trim(),
        title:x.querySelector('.next-lanes-attention-goal')?.textContent.trim() || '',
        reason:x.querySelector('.next-lanes-attention-reason')?.textContent.trim() || ''})),
      more:row.querySelector('.next-lanes-more')?.textContent.trim() || null,
      retry:!!row.querySelector('.next-lanes-retry'), goalsNeeded:row.dataset.goalsReadNeeded || null} : null;
  })()`);
}

async function stopState(ctx) {
  return ctx.session.eval(`([...document.querySelectorAll('.next-lanes-scroll .next-lanes-stop-open')]).map(button => ({
    goalId:button.dataset.goalId || null,
    number:button.querySelector('.next-lanes-stop-number')?.textContent.trim() || null,
    title:button.querySelector('.next-lanes-stop-title')?.textContent.trim() || '',
    isDefault:button.dataset.defaultGoal === 'true',
  }))`);
}

async function listItem(ctx, id) {
  return ctx.session.eval(`(() => {
    const row = document.querySelector('.next-question-list-item[data-question-id="${id}"]');
    if (!row) return null;
    const heading = row.querySelector('.next-question-list-heading');
    return {text:heading?.textContent.trim() || '', link:!!heading?.querySelector('button'),
      icon:!!heading?.querySelector('svg'), lane:heading?.querySelector('.next-question-lane-title')?.textContent.trim() || '',
      boldLane:!!heading?.querySelector('strong.next-question-lane-title'),
      goal:heading?.querySelector('.next-question-goal-title')?.textContent.trim() || '',
      own:heading?.querySelector('.next-question-heading-title')?.textContent.trim() || '',
      chat:heading?.querySelector('.next-question-chat-link')?.textContent.trim() || '',
      retry:!!heading?.querySelector('.next-question-retry'), form:!!row.querySelector('.next-question-form'),
      options:row.querySelectorAll('.next-question-option').length};
  })()`);
}

async function questionIds(ctx) {
  return ctx.session.eval("[...document.querySelectorAll('.next-question-list-item')].map(row=>row.dataset.questionId)");
}

async function cleanupRun(ctx, ownedIds, hooks, primaryError) {
  const errors = [];
  let beforeCleanup = null;
  try { beforeCleanup = await ctx.session.eval('window.__nextLanesGetEvidence?.() || null'); }
  catch (error) { errors.push({ label: 'capture pre-cleanup evidence', error }); }
  ctx.report.ok('pre-cleanup read state captured before releasing holds', !!beforeCleanup, beforeCleanup ? {
    frameSequence: beforeCleanup.frameSequence,
    renderFrame: beforeCleanup.renderFrame,
    pending: beforeCleanup.pending || [],
    calls: beforeCleanup.calls || [],
    acceptedFrames: (beforeCleanup.acceptedFrames || []).length,
  } : { error: errors.at(-1)?.error?.message || 'evidence unavailable' });
  const attempt = async (label, fn) => {
    try { await fn(); }
    catch (error) {
      errors.push(error);
      ctx.report.ok(`cleanup ${label}`, false, { name: error.name, message: error.message, stack: error.stack });
    }
  };
  await attempt('held-read release', async () => { await ctx.session.eval('window.__nextLanesReleaseAll?.()'); });
  await attempt('question', async () => { await H.cleanupQuestions(ctx, ownedIds, ctx.report); });
  for (const hook of hooks.reverse()) await attempt('document hook', async () => { await H.cleanupHook(ctx, hook); });
  await attempt('viewport', async () => { await ctx.session.send('Emulation.clearDeviceMetricsOverride'); });
  await attempt('page restore', async () => {
    await ctx.session.send('Page.navigate', { url: ctx.url });
    await ctx.session.waitFor("document.readyState === 'complete' && !!document.getElementById('session-list')", { timeoutMs: ctx.timeoutMs });
  });
  ctx.report.ok('scenario cleanup completed', errors.length === 0, { errors: errors.map(error => error.message || error.error?.message) });
  if (errors.length && primaryError) primaryError.cleanupErrors = errors.map(error => ({ name: error.name, message: error.message || error.error?.message }));
  if (errors.length && !primaryError) throw new AggregateError(errors, 'next-lanes scenario cleanup failed');
}

function isolated(run) {
  return async ctx => {
    const ownedIds = [];
    const hooks = [];
    let primaryError = null;
    try { await run(ctx, ownedIds, hooks); }
    catch (error) {
      primaryError = error;
      ctx.report.ok('scenario execution failed', false, { name: error.name, message: error.message, stack: error.stack });
      throw error;
    }
    finally { await cleanupRun(ctx, ownedIds, hooks, primaryError); }
  };
}

async function nextLanesRootRun(ctx, ownedIds, hooks) {
  const questionMemberSpecId = F.makeL11().questionSpecIds.preview;
  const q = await H.seedQuestion(ctx, ownedIds, {
    key: 'e1-preview', specId: questionMemberSpecId, title: 'Synthetic root choice',
  });
  const l11 = F.makeL11({ previewQuestionIds: [q.question_id], includeFinishedEarlier: true });
  const previewMember = l11.members.find(member => member.spec_id === questionMemberSpecId);
  const omittedQuestionGoal = l11.goals.find(goal => goal.goal_id === previewMember.goal_id);
  const ordinary = F.makeOrdinaryLane({ laneId: F.ORDINARY_A_ID, title: 'Fixture ordinary lane', memberCount: 3 });
  const quiet = F.makeQuietLane();
  const parked = F.makeParkedLane();
  const lanes = [l11, ordinary, quiet, parked];
  const frame = F.inventoryFrame(lanes);
  await install(ctx, hooks, 1440, lanes, frame, {
    holdRules: [{ laneId: F.L11_ID, membersLimit: 1, remaining: 1 }],
  });
  await wait(ctx, `!!document.querySelector(${sel(rowSelector(F.L11_ID))}) && !!document.querySelector('.next-lanes-parked-toggle')`, 'root lanes card');
  await wait(ctx, `document.querySelector('[aria-label^="Questions"]')?.dataset.count === '1'`, 'question rail count hydration');
  const rail = await ctx.session.eval(`({labels:[...document.querySelectorAll('.rail-button')].map(x=>x.getAttribute('aria-label')),
    assistantName:(()=>{const rows=window.PentacleChatStore?.getState()?.sessions||[];const visible=rows.filter(s=>s&&s.session_kind==='assistant_composite'&&s.visibility!=='hidden'&&s.closed_at==null&&s.status!=='closed');const a=visible.length===1?visible[0]:null;return a?.assistant_display_name||a?.display_name||a?.title||null;})(),
    questionsCount:document.querySelector('[aria-label^="Questions"]')?.dataset.count||null,
    lanesVisible:!document.querySelector('.lanes-card').hidden, cardTitle:!!document.querySelector('.lanes-card .next-lanes-card-title'),
    fullScreen:!!document.querySelector('.lanes-card [aria-label="Full screen"]'),
    width:document.documentElement.clientWidth, overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth,
    quiet:!!document.querySelector(${sel(rowSelector(quiet.inventory.lane_id))}),
    parkedBefore:!!document.querySelector(${sel(rowSelector(F.PARKED_ID))}),
    otherRailBadges:[...document.querySelectorAll('.rail-button')].filter(button=>!String(button.getAttribute('aria-label')||'').startsWith('Questions')
      && (button.hasAttribute('data-count')||!!button.querySelector('[data-count]'))).map(button=>button.getAttribute('aria-label')),
    rootControls:document.querySelectorAll('.next-lanes-row .next-lanes-stop-number,.next-lanes-row .next-lanes-stop-counts,.next-lanes-row .next-lanes-progress').length})`);
  check(ctx, 'six icon rail controls and desktop lanes default', rail.lanesVisible
    && rail.labels[0] === rail.assistantName
    && rail.labels[1] === 'Lanes' && rail.labels[2] === 'Chats'
    && rail.labels[3]?.startsWith('Questions')
    && rail.labels[4] === 'Limits and machines' && rail.labels[5] === 'Settings'
    && rail.labels.length === 6 && !rail.cardTitle && !rail.fullScreen, rail);
  check(ctx, 'E1 question rail data-count equals one', rail.questionsCount === '1', { questionsCount: rail.questionsCount });
  check(ctx, 'other rail controls have no question count badges', rail.otherRailBadges.length === 0, { otherRailBadges: rail.otherRailBadges });
  check(ctx, 'quiet lane is present while parked lane stays folded', rail.quiet && !rail.parkedBefore, rail);
  check(ctx, 'root rows have no goal progress, numbers, or counts', rail.rootControls === 0, rail);
  check(ctx, 'wide stage fits its 1440 px viewport without horizontal overflow', rail.width === 1440 && !rail.overflow, rail);
  const quietState = await rowState(ctx, quiet.inventory.lane_id);
  check(ctx, 'quiet row has no question mark, attention, more count, or retry', quietState?.marks === 0
    && quietState.attention.length === 0 && quietState.more === null && !quietState.retry, quietState);
  await ctx.session.click('.next-lanes-parked-toggle');
  await wait(ctx, `!!document.querySelector(${sel(rowSelector(F.PARKED_ID))})`, 'parked lane fold opens');
  check(ctx, 'parked lane appears under its fold', await ctx.session.eval(`!!document.querySelector(${sel(rowSelector(F.PARKED_ID))})`));
  await ctx.session.click('.next-lanes-parked-toggle');
  await wait(ctx, `!document.querySelector(${sel(rowSelector(F.PARKED_ID))})`, 'parked lane fold closes again');
  check(ctx, 'parked lane fold closes after opening', await ctx.session.eval(`!document.querySelector(${sel(rowSelector(F.PARKED_ID))})`));

  await wait(ctx, `window.__nextLanes.pending.some(item=>item.laneId===${sel(F.L11_ID)} && Number(item.options.membersLimit)===1)`, 'L11 goals read held');
  const pending = await rowState(ctx, F.L11_ID);
  const callsBeforeGoals = await ctx.session.eval(`window.__nextLanes.calls.filter(call=>call.laneId===${sel(F.L11_ID)}).map(call=>Number(call.options.membersLimit))`);
  const sentIds = l11.inventory.goals.map(goal => goal.goal_id);
  check(ctx, 'preview member has one mark while the exact omitted goal awaits its held goals read',
    previewMember.open_questions.count === 1 && previewMember.open_questions.first_question_id === q.question_id
      && !sentIds.includes(omittedQuestionGoal.goal_id) && pending?.marks === 1 && pending.goalsNeeded === 'true'
      && pending.attention.length === 0 && pending.more === null && JSON.stringify(callsBeforeGoals) === '[1]',
    { previewMember, omittedQuestionGoal, sentIds, pending, callsBeforeGoals });
  const releasedGoals = await ctx.session.eval(`window.__nextLanesReleaseMatching({laneId:${sel(F.L11_ID)},membersLimit:1})`);
  check(ctx, 'the held goals page is explicitly released', releasedGoals === true, { releasedGoals });
  await wait(ctx, `(() => {const b=document.querySelector(${sel(rowSelector(F.L11_ID))});const r=b?.closest('.next-lanes-row');return r?.querySelectorAll('.next-lanes-attention').length===2 && r.textContent.includes('+8 more')})()`, 'L11 lines and remaining count');
  await wait(ctx, `(() => {const b=document.querySelector(${sel(rowSelector(F.L11_ID))});const r=b?.closest('.next-lanes-row');return [...(r?.querySelectorAll('.next-lanes-attention')||[])].some(line=>line.textContent.includes('needs you'));})()`, 'previewed question appears in goal attention');
  const held = await rowState(ctx, F.L11_ID);
  const afterGoalsCalls = await ctx.session.eval(`window.__nextLanes.calls.filter(call=>call.laneId===${sel(F.L11_ID)}).map(call=>Number(call.options.membersLimit))`);
  check(ctx, 'L11 adds two ordered attention lines and exactly eight more after the goals-only read',
    held?.attention.length === 2 && held.more === '+8 more' && JSON.stringify(afterGoalsCalls) === '[1]',
    { held, afterGoalsCalls });
  check(ctx, 'previewed question resolves to the exact omitted goal ID and title',
    held?.attention[0]?.title === omittedQuestionGoal.title && held.attention[0]?.reason === 'needs you'
      && !sentIds.includes(omittedQuestionGoal.goal_id) && omittedQuestionGoal.title === 'Fixture goal 8',
    { questionGoalId: omittedQuestionGoal.goal_id, questionGoalTitle: omittedQuestionGoal.title,
      attention: held?.attention, sentIds });

  await H.cleanupHook(ctx, hooks.pop());
  const narrowHook = await H.enterNext(ctx, 900); hooks.push(narrowHook);
  await H.installLaneBridge(ctx, { frame, versions: H.laneDetailVersions(lanes) });
  await wait(ctx, 'window.innerWidth === 900 && document.querySelectorAll(".rail-button").length === 6', 'narrow desktop layout');
  const narrow = await ctx.session.eval(`({hidden:document.querySelector('.lanes-card').hidden,width:document.documentElement.clientWidth,
    overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth})`);
  check(ctx, 'lanes default closed at 900 px and the stage does not scroll sideways',
    narrow.hidden && narrow.width === 900 && !narrow.overflow, narrow);
}

async function nextLanesDrillRun(ctx, ownedIds, hooks) {
  const baseL11 = F.makeL11();
  const previewSpecId = baseL11.questionSpecIds.preview;
  const offPreviewSpecId = baseL11.questionSpecIds.offPreview;
  const questionGoalId = baseL11.members.find(member => member.spec_id === offPreviewSpecId).goal_id;
  const previewQ = await H.seedQuestion(ctx, ownedIds, {
    key: 'e2-preview', specId: previewSpecId, title: 'Synthetic preview drill choice',
  });
  const offQuestionId = `${H.QUESTION_PREFIX}e2-offpreview`;
  const completedLooseId = 'spec_gate__next_lanes_finished_earlier';
  const openLoose = F.makeMember('spec_gate__next_lanes_open_loose', null, {
    title: 'Fixture open loose spec', status: 'ready_for_dev',
  });
  const deprecatedId = 'spec_gate__next_lanes_deprecated_member';
  const extraMembers = [...Array.from({ length: 8 }, (_, index) => F.makeMember(
    `spec_gate__next_lanes_l11_extra_${String(index).padStart(2, '0')}`, questionGoalId,
    { title: `Fixture extra later member ${index + 1}`, status: 'backlog' })), openLoose,
  F.makeMember(deprecatedId, questionGoalId, { title: 'Fixture deprecated member', status: 'deprecated', terminal: 'deprecated' })];
  const extraMembersWithoutOpenLoose = extraMembers.filter(member => member.spec_id !== openLoose.spec_id);
  const detailA = F.makeL11({ previewQuestionIds: [previewQ.question_id], offPreviewQuestionIds: [offQuestionId],
    completedSpecIds: ['spec_gate__next_lanes__09'], includeFinishedEarlier: true,
    extraMembers });
  const frameA = F.inventoryFrame([detailA]);
  const l11 = detailA;
  const ordinary = F.makeOrdinaryLane({ laneId: F.ORDINARY_A_ID, title: 'Fixture drill lane', memberCount: 3 });
  const lanes = [detailA, ordinary];
  const revisions = [F.REVISION_A, F.REVISION_B, F.REVISION_C, F.REVISION_D];
  const versionMap = { [F.L11_ID]: Object.fromEntries(revisions.map(revision => [revision,
    revision === F.REVISION_A ? detailA : F.makeL11({ revision, previewQuestionIds: [previewQ.question_id],
      offPreviewQuestionIds: [offQuestionId], completedSpecIds: ['spec_gate__next_lanes__09'],
      includeFinishedEarlier: true, extraMembers })])),
    [F.ORDINARY_A_ID]: { [F.REVISION_A]: ordinary } };
  await install(ctx, hooks, 1440, lanes, frameA, {
    versions: versionMap,
    holdRules: [
      { laneId: F.L11_ID, membersLimit: 1, remaining: 1 },
      { laneId: F.L11_ID, membersLimit: 32, remaining: 1 },
    ],
  });
  await wait(ctx, `window.__nextLanes.pending.some(item=>item.laneId===${sel(F.L11_ID)} && Number(item.options.membersLimit)===1)`, 'initial L11 goals read');
  const goalsPending = await ctx.session.eval(`window.__nextLanes.pending.filter(item=>item.laneId===${sel(F.L11_ID)}).map(item=>Number(item.options.membersLimit))`);
  check(ctx, 'preview attribution starts the held goals read before the off-preview question exists',
    JSON.stringify(goalsPending) === '[1]', { goalsPending, calls: await laneCallEvidence(ctx, F.L11_ID) });
  const offQ = await H.seedQuestion(ctx, ownedIds, {
    key: 'e2-offpreview', specId: offPreviewSpecId, title: 'Synthetic drill choice',
  });
  check(ctx, 'off-preview durable question was created after the goals request was staged',
    offQ.question_id === offQuestionId && offQ.spec_id === offPreviewSpecId,
    { questionId: offQ.question_id, specId: offQ.spec_id, previewQuestionId: previewQ.question_id });
  const q = offQ;
  await wait(ctx, `window.__nextLanes.pending.some(item=>item.laneId===${sel(F.L11_ID)} && Number(item.options.membersLimit)===32)`, 'off-preview L11 members read held after goals');
  const stagedCalls = await laneCallEvidence(ctx, F.L11_ID);
  check(ctx, 'independent goals and members reads are both held in request order', stagedCalls.length === 2
    && stagedCalls[0].membersLimit === 1 && stagedCalls[1].membersLimit === 32
    && stagedCalls.every(call => call.revision === F.REVISION_A), stagedCalls);
  const releasedGoals = await ctx.session.eval(`window.__nextLanesReleaseMatching({laneId:${sel(F.L11_ID)},membersLimit:1})`);
  check(ctx, 'the staged goals read is explicitly released before lane navigation', releasedGoals === true, { releasedGoals });
  await ctx.session.click(rowSelector(F.L11_ID));
  await wait(ctx, 'document.querySelector(".next-lanes-scroll")?.dataset.level === "lane"', 'lane view');
  await wait(ctx, `window.__nextLanes.pending.some(item=>item.laneId===${sel(F.L11_ID)} && Number(item.options.membersLimit)===32)`, 'L11 members read held');
  const before = await ctx.session.eval(`({stops:document.querySelectorAll('.next-lanes-stop-open').length,
    more:document.querySelector('.next-lanes-more-stops')?.textContent || '',
    lastDefault:[...document.querySelectorAll('.next-lanes-stop-open')].at(-1)?.dataset.defaultGoal==='true'})`);
  check(ctx, 'capped lane shows eight inventory goals, three more, and open no-goal work last before members arrive',
    before.stops === 9 && before.more === '+3 more' && before.lastDefault, before);
  const previewStops = await stopState(ctx);
  const expectedPreviewStops = l11.inventory.goals.map(goal => ({ goalId: goal.goal_id, number: String(goal.pos + 1) }));
  check(ctx, 'before detail, the capped stop IDs and numbers exactly match the eight inventory goals',
    JSON.stringify(previewStops.slice(0, 8).map(stop => ({ goalId: stop.goalId, number: stop.number }))) === JSON.stringify(expectedPreviewStops)
      && previewStops[8]?.isDefault,
    { previewStops, expectedPreviewStops });
  const releasedMembers = await ctx.session.eval(`window.__nextLanesReleaseMatching({laneId:${sel(F.L11_ID)},membersLimit:32})`);
  check(ctx, 'the first held member page is explicitly released', releasedMembers === true, { releasedMembers });
  await wait(ctx, `window.__nextLanes.calls.some(call=>call.laneId===${sel(F.L11_ID)} && Number(call.options.membersLimit)===32 && call.status==='resolved')`, 'initial L11 members accepted');
  await wait(ctx, 'document.querySelectorAll(".next-lanes-stop-open").length === 12', 'all L11 stops and no-goal work after members read');
  const laneDetailStops = await stopState(ctx);
  const expectedFullStops = [...l11.goals.map(goal => ({ goalId: goal.goal_id, number: String(goal.pos + 1), isDefault: false })),
    { goalId: null, number: '12', isDefault: true }];
  check(ctx, 'all eleven numbered goals and the open no-goal stop appear last after all member pages',
    JSON.stringify(laneDetailStops.map(stop => ({ goalId: stop.goalId, number: stop.number, isDefault: stop.isDefault }))) === JSON.stringify(expectedFullStops),
    { laneDetailStops, expectedFullStops });
  const openLooseDone = await ctx.session.eval(`({defaultStop:!!document.querySelector('.next-lanes-stop.default'),
    finishedEarlier:!!document.querySelector('.next-lanes-finished-toggle'),
    doneLabel:document.querySelector('.next-lanes-stop.default .next-lanes-section-done .next-lanes-section-toggle')?.textContent.trim() || ''})`);
  check(ctx, 'open no-goal work keeps completed loose work inside its default stop Done fold',
    openLooseDone.defaultStop && !openLooseDone.finishedEarlier && openLooseDone.doneLabel.startsWith('Done · 1'), openLooseDone);
  await ctx.session.click('.next-lanes-stop.default .next-lanes-section-done .next-lanes-section-toggle');
  await wait(ctx, `!!document.querySelector('.next-lanes-stop.default .next-lanes-section-done .next-lanes-member-title[data-spec-id="${completedLooseId}"]')`, 'default stop Done member appears');
  check(ctx, 'default stop Done fold expands the exact completed loose member',
    await ctx.session.eval(`!!document.querySelector('.next-lanes-stop.default .next-lanes-section-done .next-lanes-member-title[data-spec-id="${completedLooseId}"]')`));
  await ctx.session.click('.next-lanes-stop.default .next-lanes-section-done .next-lanes-section-toggle');
  await ctx.session.eval('window.__nextLanesReleaseAll()');

  const qGoalSelector = `.next-lanes-stop-open[data-goal-id="${questionGoalId}"]`;
  await ctx.session.click(qGoalSelector);
  await wait(ctx, 'document.querySelector(".next-lanes-scroll")?.dataset.level === "goal"', 'goal view');
  await wait(ctx, `window.__nextLanes.calls.some(call=>call.laneId===${sel(F.L11_ID)} && call.options.goalId===${sel(questionGoalId)} && call.status==='resolved')`, 'exact goal members reply accepted');
  const goalDetail = await ctx.session.eval(`(() => ({back:document.querySelector('.next-lanes-back')?.textContent || '',
    sections:[...document.querySelectorAll('.next-lanes-section')].map(el=>el.className.match(/next-lanes-section-([a-z_]+)/)?.[1]),
    firstToggle:!!document.querySelector('.next-lanes-section-toggle'),
    laterTotal:document.querySelector('.next-lanes-section-later')?.querySelectorAll('.next-lanes-member-title').length || 0,
    laterMore:document.querySelector('.next-lanes-section-later .next-lanes-show-more')?.textContent.trim() || null,
    deprecatedVisible:!!document.querySelector('.next-lanes-member-title[data-spec-id="${deprecatedId}"]')}))()`);
  const order = ['working', 'needs', 'in_progress', 'blocked', 'next', 'later', 'done'];
  const sorted = [...goalDetail.sections].sort((a,b)=>order.indexOf(a)-order.indexOf(b));
  check(ctx, 'goal detail names its lane in Back and keeps member sections in order', goalDetail.back.includes('Fixture lane eleven')
    && JSON.stringify(goalDetail.sections) === JSON.stringify(sorted), goalDetail);
  check(ctx, 'goal detail has exact Needs, Done, and capped Later membership evidence',
    goalDetail.sections.includes('needs') && goalDetail.sections.includes('done') && goalDetail.laterTotal === 6
      && goalDetail.laterMore === 'Show 2 more' && !goalDetail.deprecatedVisible, goalDetail);
  await ctx.session.click('.next-lanes-section-later .next-lanes-show-more');
  await wait(ctx, `document.querySelectorAll('.next-lanes-section-later .next-lanes-member-title').length === 8`, 'Later members expand to eight');
  check(ctx, 'Later section expands all eight fixture members', await ctx.session.eval(`document.querySelectorAll('.next-lanes-section-later .next-lanes-member-title').length === 8`));
  check(ctx, 'expanded Later section has no extra collapse control, matching the mock',
    await ctx.session.eval(`!document.querySelector('.next-lanes-section-later .next-lanes-show-more')`));
  if (goalDetail.firstToggle) {
    await ctx.session.click('.next-lanes-section-toggle');
    await wait(ctx, '!!document.querySelector(".next-lanes-section-done .next-lanes-member-list")', 'Done fold opens');
    check(ctx, 'Done member fold opens and includes the completed member', await ctx.session.eval(`!!document.querySelector('.next-lanes-section-done .next-lanes-member-title[data-spec-id="spec_gate__next_lanes__09"]')`));
    await ctx.session.click('.next-lanes-section-toggle');
    await wait(ctx, '!document.querySelector(".next-lanes-section-done .next-lanes-member-list")', 'Done fold closes');
    check(ctx, 'Done member fold closes', true);
  } else check(ctx, 'Done member fold exists for the synthetic completed member', false, goalDetail);
  const memberSelector = `.next-lanes-member-title[data-spec-id="${q.spec_id}"]`;
  await ctx.session.click(memberSelector);
  await wait(ctx, 'document.querySelector(".next-lanes-scroll")?.dataset.level === "spec"', 'spec view');
  const spec = await ctx.session.eval(`(() => ({back:document.querySelector('.next-lanes-back')?.textContent || '',
    title:document.querySelector('.next-lanes-spec-title')?.textContent || '', stage:document.querySelector('.next-lanes-stage')?.textContent || '',
    checks:!!document.querySelector('.next-lanes-checks'), recent:!!document.querySelector('.next-lanes-recent'),
    facts:document.querySelectorAll('.next-lanes-fact').length, passed:!!document.querySelector('.next-lanes-passed-toggle'),
    openChecks:[...document.querySelectorAll('.next-lanes-criteria-left li')].map(row=>row.textContent),
    recentRows:document.querySelectorAll('.next-lanes-recent li').length}))()`);
  check(ctx, 'spec view shows its chip, title, facts, checks, Recent, and exact goal Back label', spec.title.length > 0
    && spec.stage.length > 0 && spec.checks && spec.recent && spec.facts > 0
    && spec.back.includes(l11.goals.find(goal => goal.goal_id === questionGoalId).title)
    && spec.openChecks.includes('Synthetic open check'), spec);
  if (spec.passed) {
    await ctx.session.click('.next-lanes-passed-toggle');
    await wait(ctx, '!!document.querySelector(".next-lanes-criteria-passed")', 'passed checks fold opens');
    check(ctx, 'passed checks fold opens', true);
    await ctx.session.click('.next-lanes-passed-toggle');
    await wait(ctx, '!document.querySelector(".next-lanes-criteria-passed")', 'passed checks fold closes');
    check(ctx, 'passed checks fold closes', true);
  } else check(ctx, 'passed checks fold exists', false, spec);
  await ctx.session.click('.next-lanes-back');
  await wait(ctx, 'document.querySelector(".next-lanes-scroll")?.dataset.level === "goal"', 'spec Back returns to goal');
  const goalBack = await ctx.session.eval(`document.querySelector('.next-lanes-back')?.textContent || ''`);
  check(ctx, 'goal Back label names its lane', goalBack.includes(l11.inventory.title), { goalBack });
  await ctx.session.click('.next-lanes-back');
  await wait(ctx, 'document.querySelector(".next-lanes-scroll")?.dataset.level === "lane"', 'goal Back returns to lane');
  const laneBack = await ctx.session.eval(`document.querySelector('.next-lanes-back')?.textContent || ''`);
  check(ctx, 'lane Back label names All lanes', laneBack.includes('All lanes'), { laneBack });
  await ctx.session.click('.next-lanes-back');
  await wait(ctx, 'document.querySelector(".next-lanes-scroll")?.dataset.level === "all"', 'lane Back returns to All lanes');
  check(ctx, 'lane Back restores the All lanes view', await ctx.session.eval(`document.querySelector('.next-lanes-scroll')?.dataset.level==='all'`));

  // Completed loose work becomes Finished earlier only in a separately staged
  // frame with no open default-goal member.
  await H.cleanupHook(ctx, hooks.pop());
  const finishedOnlyLane = F.makeL11({ previewQuestionIds: [previewQ.question_id], offPreviewQuestionIds: [offQuestionId],
    completedSpecIds: ['spec_gate__next_lanes__09'], includeFinishedEarlier: true,
    extraMembers: extraMembersWithoutOpenLoose });
  await install(ctx, hooks, 1440, [finishedOnlyLane], F.inventoryFrame([finishedOnlyLane]), {
    holdRules: [{ laneId: F.L11_ID, membersLimit: 32, remaining: 1 }],
  });
  await ctx.session.click(rowSelector(F.L11_ID));
  await wait(ctx, `window.__nextLanes.pending.some(item=>item.laneId===${sel(F.L11_ID)} && Number(item.options.membersLimit)===32)`, 'finished-only members read held');
  const finishedOnlyHeld = await laneCallEvidence(ctx, F.L11_ID);
  check(ctx, 'finished-only frame holds the current revision members read before placement evidence',
    finishedOnlyHeld.length === 1 && finishedOnlyHeld[0].membersLimit === 32
      && finishedOnlyHeld[0].revision === F.REVISION_A && finishedOnlyHeld[0].status === 'pending', finishedOnlyHeld);
  const beforeFinishedRelease = await ctx.session.eval(`({finishedEarlier:!!document.querySelector('.next-lanes-finished-toggle'),
    defaultStop:!!document.querySelector('.next-lanes-stop.default')})`);
  check(ctx, 'finished-only placement waits for the held full member read',
    !beforeFinishedRelease.finishedEarlier && !beforeFinishedRelease.defaultStop, beforeFinishedRelease);
  const releasedFinishedOnly = await ctx.session.eval(`window.__nextLanesReleaseMatching({laneId:${sel(F.L11_ID)},membersLimit:32})`);
  check(ctx, 'the finished-only members read is explicitly released', releasedFinishedOnly === true, { releasedFinishedOnly });
  await wait(ctx, `window.__nextLanes.calls.some(call=>call.laneId===${sel(F.L11_ID)} && Number(call.options.membersLimit)===32 && call.status==='resolved')`, 'finished-only members accepted');
  await wait(ctx, `!!document.querySelector('.next-lanes-finished-toggle') && !document.querySelector('.next-lanes-stop.default')`, 'Finished earlier root fold appears after the completed read');
  const finishedEarlier = await ctx.session.eval(`({label:document.querySelector('.next-lanes-finished-toggle')?.textContent.trim() || '',
    count:document.querySelector('.next-lanes-finished-toggle')?.textContent.match(/Finished earlier · (\\d+)/)?.[1] || null,
    defaultStop:!!document.querySelector('.next-lanes-stop.default')})`);
  check(ctx, 'without open no-goal work, the completed loose member appears in Finished earlier once',
    finishedEarlier.count === '1' && finishedEarlier.label.startsWith('Finished earlier · 1') && !finishedEarlier.defaultStop, finishedEarlier);
  await ctx.session.click('.next-lanes-finished-toggle');
  await wait(ctx, `!!document.querySelector('.next-lanes-section-done .next-lanes-section-toggle')`, 'Finished earlier Done fold');
  await ctx.session.click('.next-lanes-section-done .next-lanes-section-toggle');
  await wait(ctx, `!!document.querySelector('.next-lanes-section-done .next-lanes-member-title[data-spec-id="${completedLooseId}"]')`, 'Finished earlier member appears');
  check(ctx, 'Finished earlier Done fold expands the exact completed loose member',
    await ctx.session.eval(`!!document.querySelector('.next-lanes-section-done .next-lanes-member-title[data-spec-id="${completedLooseId}"]')`));
  await ctx.session.click('.next-lanes-section-done .next-lanes-section-toggle');
  await ctx.session.click('.next-lanes-finished-toggle');
  const finishedClosed = await ctx.session.eval(`({expanded:document.querySelector('.next-lanes-finished-toggle')?.getAttribute('aria-expanded')==='true',
    doneSections:document.querySelectorAll('.next-lanes-section-done').length})`);
  check(ctx, 'Finished earlier closes after its Done fold closes', !finishedClosed.expanded && finishedClosed.doneSections === 0, finishedClosed);

  // A fresh page gives the read controller a known A start for two successive
  // revision changes while a members read is in flight.
  await H.cleanupHook(ctx, hooks.pop());
  const revisionHook = await H.enterNext(ctx, 1440); hooks.push(revisionHook);
  const revisionLanes = revisions.map(revision => F.makeL11({ revision, previewQuestionIds: [previewQ.question_id],
    offPreviewQuestionIds: [offQuestionId], completedSpecIds: ['spec_gate__next_lanes__09'],
    includeFinishedEarlier: true, extraMembers }));
  await H.installLaneBridge(ctx, { frame: F.inventoryFrame([revisionLanes[0]]), versions: {
    [F.L11_ID]: Object.fromEntries(revisionLanes.map(lane => [lane.detailRevision, lane])),
  }, holdRules: [{ laneId: F.L11_ID, membersLimit: 32, remaining: 2 }] });
  await ctx.session.click(rowSelector(F.L11_ID));
  await wait(ctx, `window.__nextLanes.pending.length === 1`, 'revision A members request held');
  for (const lane of revisionLanes.slice(1, 3)) {
    await setInventory(ctx, F.inventoryFrame([lane]));
    if (lane.detailRevision === F.REVISION_B) {
      await wait(ctx, `window.__nextLanes.calls.filter(call=>call.laneId===${sel(F.L11_ID)} && Number(call.options.membersLimit)===32).length === 2`, 'revision B members request restarted');
    }
  }
  await wait(ctx, `!!document.querySelector('.next-lanes-retry[data-lane-id="${F.L11_ID}"][data-read-kind="members"]')`, 'Retry appears after repeated revisions');
  const revisionCalls = await ctx.session.eval(`window.__nextLanes.calls.filter(call=>call.laneId===${sel(F.L11_ID)} && Number(call.options.membersLimit)===32).length`);
  check(ctx, 'two successive revision changes fail closed and expose Retry', revisionCalls === 2, { revisionCalls });
  await ctx.session.eval('window.__nextLanesReleaseAll()');
  await ctx.session.click(`.next-lanes-retry[data-lane-id="${F.L11_ID}"][data-read-kind="members"]`);
  await wait(ctx, `window.__nextLanes.calls.some(call=>call.laneId===${sel(F.L11_ID)} && call.revision===${sel(F.REVISION_C)} && call.status==='resolved')`, 'Retry reads the current revision C');
  await wait(ctx, 'document.querySelectorAll(".next-lanes-stop-open").length === 12 && !document.querySelector(".next-lanes-retry[data-read-kind=members]")', 'Retry reloads current revision');
  const retryCall = await ctx.session.eval(`window.__nextLanes.calls.filter(call=>call.laneId===${sel(F.L11_ID)} && call.status==='resolved').at(-1)`);
  check(ctx, 'Retry recovers the exact current revision and restores all stops', retryCall?.revision === F.REVISION_C
    && await ctx.session.eval('document.querySelectorAll(".next-lanes-stop-open").length === 12'), retryCall);
}

async function nextQuestionsListRun(ctx, ownedIds, hooks) {
  const l11Base = F.makeL11();
  const ordinaryBase = F.makeOrdinaryLane({ laneId: F.ORDINARY_A_ID, title: 'Fixture question lane',
    goalTitle: 'Fixture question goal', memberCount: 3 });
  const noGoal = F.makeOrdinaryLane({ laneId: F.NO_GOAL_ID, title: 'Fixture no-goal lane', memberCount: 1, noGoal: true });
  const q1Spec = ordinaryBase.members[0].spec_id;
  const q2Spec = noGoal.members[0].spec_id;
  const qPreviewSpec = l11Base.questionSpecIds.preview;
  const qOffSpec = l11Base.questionSpecIds.offPreview;
  const questions = [];
  const fillers = [];
  const goalQ = await H.seedQuestion(ctx, ownedIds, { key: 'e3-goal', specId: q1Spec, title: 'Synthetic goal question' });
  const laneQ = await H.seedQuestion(ctx, ownedIds, { key: 'e3-no-goal', specId: q2Spec, title: 'Synthetic lane question' });
  const previewQ = await H.seedQuestion(ctx, ownedIds, { key: 'e3-preview', specId: qPreviewSpec, title: 'Synthetic capped question' });
  const chatQ = await H.seedQuestion(ctx, ownedIds, { key: 'e3-chat', title: 'Synthetic chat question' });
  const unlistedQ = await H.seedQuestion(ctx, ownedIds, { key: 'e3-no-chat', title: 'Synthetic unlisted question', producerStreamId: H.UNLISTED_PRODUCER });
  questions.push(goalQ, laneQ, previewQ, chatQ, unlistedQ);
  for (let index = 0; index < 96; index += 1) {
    fillers.push(await H.seedQuestion(ctx, ownedIds, {
      key: `e3-cap-${String(index).padStart(2, '0')}`,
      title: `Synthetic capped-list filler ${index + 1}`,
      producerStreamId: H.UNLISTED_PRODUCER,
    }));
  }
  const offQuestionId = `${H.QUESTION_PREFIX}e3-off-preview`;
  const orphanQuestionId = `${H.QUESTION_PREFIX}e3-orphan`;
  const l11Frame = F.makeL11({ previewQuestionIds: [previewQ.question_id] });
  const l11Detail = F.makeL11({ previewQuestionIds: [previewQ.question_id], offPreviewQuestionIds: [offQuestionId] });
  const l11 = l11Detail;
  const ordinary = F.makeOrdinaryLane({ laneId: F.ORDINARY_A_ID, title: 'Fixture question lane', goalTitle: 'Fixture question goal',
    memberCount: 3, questionSpecIds: [q1Spec], questionIdsBySpec: { [q1Spec]: [goalQ.question_id] } });
  const goalQuestionMember = ordinary.members.find(member => member.spec_id === q1Spec);
  const noGoalWithQuestion = F.makeOrdinaryLane({ laneId: F.NO_GOAL_ID, title: 'Fixture no-goal lane', memberCount: 1,
    noGoal: true, questionSpecIds: [q2Spec], questionIdsBySpec: { [q2Spec]: [laneQ.question_id] } });
  const noGoalQuestionMember = noGoalWithQuestion.members.find(member => member.spec_id === q2Spec);
  const previewQuestionMember = l11.members.find(member => member.spec_id === qPreviewSpec);
  const offPreviewQuestionMember = l11.members.find(member => member.spec_id === qOffSpec);
  check(ctx, 'question navigation targets have fixture titles', !!goalQuestionMember?.title && !!noGoalQuestionMember?.title
    && !!previewQuestionMember?.title && !!offPreviewQuestionMember?.title,
    { goal: goalQuestionMember?.title, noGoal: noGoalQuestionMember?.title,
      preview: previewQuestionMember?.title, offPreview: offPreviewQuestionMember?.title });
  check(ctx, 'no-goal question uses the fixture-generated spec with an explicit null goal',
    !!noGoalQuestionMember && noGoalQuestionMember.goal_id === null, { questionSpecId: q2Spec, member: noGoalQuestionMember });
  const lanes = [l11Detail, ordinary, noGoalWithQuestion, F.makeQuietLane()];
  const e3Hook = await H.enterNext(ctx, 1440);
  hooks.push(e3Hook);
  await wait(ctx, `window.__nextLanesPromptReads.some(read=>read.args?.open===true && read.status==='resolved' && read.questionIds.length===101)`,
    'startup open prompt.list hydrates the complete pre-entry set');
  await wait(ctx, `document.querySelector('[aria-label="Questions (101)"]')`, 'startup question count includes the uncapped open set');
  const notificationPage = await ctx.session.eval(`window.cc.notificationList({ states: ['open'] })`);
  const notificationPageIds = (notificationPage?.notifications || []).map(item => item?.question?.question_id || item?.question_id);
  const startupSeededIds = [...questions, ...fillers].map(question => question.question_id);
  const missingNotificationIds = startupSeededIds.filter(id => !notificationPageIds.includes(id));
  const omittedQuestionId = missingNotificationIds[0];
  const startupOpenRead = await ctx.session.eval(`window.__nextLanesPromptReads.find(read=>read.args?.open===true && read.status==='resolved' && read.questionIds.length===101) || null`);
  check(ctx, 'startup notification.list is capped while the actual startup prompt.list includes its omitted durable question',
    notificationPageIds.length === 100 && missingNotificationIds.length === 1
      && startupOpenRead?.questionIds.includes(omittedQuestionId)
      && startupSeededIds.every(id => startupOpenRead.questionIds.includes(id)),
    { notificationCount: notificationPageIds.length, missingNotificationIds, startupOpenRead });
  await openList(ctx, 101);
  check(ctx, 'the question omitted from the capped notification snapshot is present in the hydrated startup list',
    (await questionIds(ctx)).includes(omittedQuestionId), { omittedQuestionId });
  await ctx.session.click('[aria-label^="Questions"]');
  await wait(ctx, `(() => {
    const button=document.querySelector('[aria-label^="Questions"]');
    const popover=document.querySelector('.popover.questions-popover');
    return !!button && !button.classList.contains('open') && !!popover?.hidden && popover.childElementCount === 0;
  })()`, 'startup question popover closes, empties, and becomes inactive before lane placement staging');

  await H.installLaneBridge(ctx, { frame: F.inventoryFrame([l11Frame, ordinary, noGoalWithQuestion, F.makeQuietLane()]),
    versions: H.laneDetailVersions(lanes),
    holdRules: [
      { laneId: F.L11_ID, membersLimit: 1, remaining: 1 },
      { laneId: F.L11_ID, membersLimit: 32, remaining: 1 },
    ],
  });
  await wait(ctx, `window.__nextLanes.pending.some(item=>item.laneId===${sel(F.L11_ID)} && Number(item.options.membersLimit)===1)`, 'previewed L11 question holds goals first');
  const goalsBeforeOffPreview = await ctx.session.eval(`window.__nextLanes.pending.filter(item=>item.laneId===${sel(F.L11_ID)}).map(item=>Number(item.options.membersLimit))`);
  check(ctx, 'off-preview and orphan questions are delayed until after the preview goals request is held',
    JSON.stringify(goalsBeforeOffPreview) === '[1]', { goalsBeforeOffPreview });
  const offQ = await H.seedQuestion(ctx, ownedIds, { key: 'e3-off-preview', specId: qOffSpec, title: 'Synthetic hidden member question' });
  const orphanQ = await H.seedQuestion(ctx, ownedIds, { key: 'e3-orphan', specId: 'spec_gate__next_lanes_orphan', title: 'Synthetic unplaced question' });
  questions.push(offQ, orphanQ);
  check(ctx, 'off-preview question producer matches the staged full-detail fixture', offQ.question_id === offQuestionId && offQ.spec_id === qOffSpec,
    { questionId: offQ.question_id, specId: offQ.spec_id });
  await wait(ctx, `window.__nextLanes.pending.some(item=>item.laneId===${sel(F.L11_ID)} && Number(item.options.membersLimit)===32)`, 'off-preview L11 membership read held');
  const seededIds = [...questions, ...fillers].map(question => question.question_id);
  await wait(ctx, `document.querySelector('[aria-label="Questions (103)"]')`, 'all durable questions appear after the two staged placements are seeded');
  await openList(ctx, 103);
  const pendingGoals = await listItem(ctx, previewQ.question_id);
  check(ctx, 'capped preview question keeps its own title until goals are held', pendingGoals?.own === 'Synthetic capped question' && !pendingGoals.link);
  await wait(ctx, `window.__nextLanes.pending.some(item=>item.laneId===${sel(F.L11_ID)} && Number(item.options.membersLimit)===1)`, 'L11 goals read held');
  const orphanBeforeReads = await listItem(ctx, orphanQ.question_id);
  check(ctx, 'orphan question has its own title and no Retry while L11 reads are held', orphanBeforeReads?.own === 'Synthetic unplaced question'
    && !orphanBeforeReads.link && !orphanBeforeReads.retry, orphanBeforeReads);
  const releasedGoals = await ctx.session.eval(`window.__nextLanesReleaseMatching({laneId:${sel(F.L11_ID)},membersLimit:1})`);
  check(ctx, 'the capped preview goals read is explicitly released', releasedGoals === true, { releasedGoals });
  await wait(ctx, `!!document.querySelector('.next-question-list-item[data-question-id="${previewQ.question_id}"] .next-question-heading-link .next-question-goal-title')`, 'preview question placement');

  await wait(ctx, `window.__nextLanes.pending.some(item=>item.laneId===${sel(F.L11_ID)} && Number(item.options.membersLimit)===32)`, 'off-preview L11 member read held');
  const ids = await questionIds(ctx);
  check(ctx, 'question list contains all 103 durable open questions', ids.length === 103
    && [...questions, ...fillers].every(q => ids.includes(q.question_id)), { count: ids.length });
  const heldOff = await listItem(ctx, offQ.question_id);
  check(ctx, 'L11 off-preview question keeps its own title while membership is held', heldOff?.own === 'Synthetic hidden member question' && !heldOff.link && !heldOff.retry, heldOff);

  const listGoal = await listItem(ctx, goalQ.question_id);
  const listChat = await listItem(ctx, chatQ.question_id);
  const listNoGoal = await listItem(ctx, laneQ.question_id);
  const listPreview = await listItem(ctx, previewQ.question_id);
  const listUnlisted = await listItem(ctx, unlistedQ.question_id);
  const listOrphan = await listItem(ctx, orphanQ.question_id);
  check(ctx, 'goal question heading includes its lane icon, bold lane, and goal', !!listGoal?.link && listGoal.icon && listGoal.boldLane
    && listGoal.lane === ordinary.inventory.title && listGoal.goal === ordinary.goals[0].title, listGoal);
  check(ctx, 'no-spec question from a listed chat uses the listed chat title and links to that chat',
    !!listChat?.chat && listChat.link && !listChat.own, listChat);
  check(ctx, 'no-goal spec question heading includes its lane icon, bold lane, and lane only', !!listNoGoal?.link && listNoGoal.icon
    && listNoGoal.boldLane && listNoGoal.lane === noGoalWithQuestion.inventory.title && !listNoGoal.goal, listNoGoal);
  check(ctx, 'preview question reaches its uncapped goal after the goals read is held', listPreview?.lane === l11.inventory.title
    && listPreview.goal === l11.goals[7].title, listPreview);
  check(ctx, 'no-spec question with an unlisted producer has its own unlinked title', listUnlisted?.own === 'Synthetic unlisted question'
    && !listUnlisted.link && !listUnlisted.retry, listUnlisted);
  check(ctx, 'orphan spec question keeps its own title without Retry after read evidence', listOrphan?.own === 'Synthetic unplaced question'
    && !listOrphan.link && !listOrphan.retry, listOrphan);
  const formStates = [];
  for (const q of questions) formStates.push(await ctx.session.eval(`(() => {
    const row=document.querySelector('.next-question-list-item[data-question-id="${q.question_id}"]');
    return !!row?.querySelector('.next-question-form') && !!row?.querySelector('.next-question-send')
      && !row.querySelector('.next-question-skip,[aria-label*="Voice"],button[class*="voice"]');
  })()`));
  check(ctx, 'all seven representative questions have a live answer form and no skip or voice control', formStates.every(Boolean),
    await ctx.session.eval(`(() => ({forms:document.querySelectorAll('.next-question-list-item .next-question-form').length,
      controls:document.querySelectorAll('.next-question-skip,[aria-label*="Voice"],button[class*="voice"]').length}))()`));

  const releasedMembers = await ctx.session.eval(`window.__nextLanesReleaseMatching({laneId:${sel(F.L11_ID)},membersLimit:32})`);
  check(ctx, 'the held L11 member page is explicitly released', releasedMembers === true, { releasedMembers });
  await wait(ctx, `!!document.querySelector('.next-question-list-item[data-question-id="${offQ.question_id}"] .next-question-heading-link .next-question-goal-title')`, 'off-preview question placement held');
  await wait(ctx, `window.__nextLanes.calls.filter(call=>call.laneId===${sel(F.L11_ID)} && Number(call.options.membersLimit)===32).every(call=>call.status==='resolved')`, 'all L11 membership pages accepted');
  const placedOff = await listItem(ctx, offQ.question_id);
  check(ctx, 'off-preview question receives its lane and goal after member pages are held', placedOff?.lane === l11.inventory.title
    && placedOff.goal === l11.goals[2].title, placedOff);
  const orphanAfterReads = await listItem(ctx, orphanQ.question_id);
  check(ctx, 'orphan question remains unlinked and Retry-free after every applicable member page answers',
    orphanAfterReads?.own === 'Synthetic unplaced question' && !orphanAfterReads.link && !orphanAfterReads.retry,
    { orphanBeforeReads, orphanAfterReads, reads: await ctx.session.eval('window.__nextLanes.calls') });

  // Every form is exercised by selecting and unselecting its first option.
  for (const question of questions) {
    const option = `.next-question-list-item[data-question-id="${question.question_id}"] .next-question-option`;
    await ctx.session.click(option);
    const enabled = await ctx.session.eval(`!document.querySelector(${sel(`.next-question-list-item[data-question-id="${question.question_id}"] .next-question-send`)}).disabled`);
    await ctx.session.click(option);
    const disabled = await ctx.session.eval(`document.querySelector(${sel(`.next-question-list-item[data-question-id="${question.question_id}"] .next-question-send`)}).disabled`);
    check(ctx, `question ${question.question_id} form accepts and clears a choice`, enabled && disabled, { enabled, disabled });
  }
  check(ctx, 'question list contains no skip or voice controls', await ctx.session.eval('!document.querySelector(".next-questions-list .next-question-skip,.next-questions-list [aria-label*=Voice],.next-questions-list button[class*=voice]")'));

  await ctx.session.click('.next-lanes-close');
  await openList(ctx, 103);
  const cardClosedBeforeGoalNavigation = await ctx.session.eval('document.querySelector(".lanes-card")?.hidden === true');
  await ctx.session.click(`.next-question-heading-link[data-testid="next-question-heading-${goalQ.question_id}"]`);
  await wait(ctx, `document.querySelector('.next-lanes-scroll')?.dataset.level === 'spec' && document.querySelector('.next-lanes-spec-title')?.textContent === ${sel(goalQuestionMember.title)}`, 'goal heading opens its fixture-titled spec after lanes card was closed');
  await wait(ctx, `!!document.querySelector('.next-lanes-scroll .next-question-card[data-question-id="${goalQ.question_id}"]')`, 'goal question card mounts at spec level');
  const goalCardControls = await ctx.session.eval(`(() => {const card=document.querySelector('.next-lanes-scroll .next-question-card[data-question-id="${goalQ.question_id}"]');return !!card?.querySelector('.next-question-form')
    && !card.querySelector('.next-question-skip,[aria-label*="Voice"],button[class*="voice"]')})()`);
  check(ctx, 'goal question card has an answer form without skip or voice controls', goalCardControls);
  check(ctx, 'goal heading opens its spec after the lanes card was closed', cardClosedBeforeGoalNavigation
    && await ctx.session.eval(`!document.querySelector('.lanes-card').hidden && document.querySelector('.next-lanes-scroll').dataset.level==='spec'`));
  await ctx.session.click('[aria-label^="Questions"]');
  await wait(ctx, "!!document.querySelector('.next-questions-list')", 'question list reopens');
  await ctx.session.click('.next-lanes-close');
  const cardClosedBeforeNoGoalNavigation = await ctx.session.eval('document.querySelector(".lanes-card")?.hidden === true');
  await openList(ctx, 103);
  await ctx.session.click(`.next-question-heading-link[data-testid="next-question-heading-${laneQ.question_id}"]`);
  await wait(ctx, `document.querySelector('.next-lanes-scroll')?.dataset.level === 'spec' && document.querySelector('.next-lanes-spec-title')?.textContent === ${sel(noGoalQuestionMember.title)}
    && !document.querySelector('.lanes-card').hidden`, 'no-goal heading opens the visible lanes card at its spec');
  await wait(ctx, `!!document.querySelector('.next-lanes-scroll .next-question-card[data-question-id="${laneQ.question_id}"]')`, 'no-goal question card mounts at spec level');
  check(ctx, 'no-goal question card has an answer form without skip or voice controls', await ctx.session.eval(`(() => {const card=document.querySelector('.next-lanes-scroll .next-question-card[data-question-id="${laneQ.question_id}"]');return !!card?.querySelector('.next-question-form')
    && !card.querySelector('.next-question-skip,[aria-label*="Voice"],button[class*="voice"]')})()`));
  check(ctx, 'no-goal heading opens its spec after the lanes card was closed', cardClosedBeforeNoGoalNavigation
    && await ctx.session.eval(`!document.querySelector('.lanes-card').hidden && document.querySelector('.next-lanes-scroll').dataset.level==='spec'`));
  await ctx.session.click('[aria-label^="Questions"]');
  await wait(ctx, "!!document.querySelector('.next-questions-list')", 'question list reopens');
  await ctx.session.click('.next-lanes-close');
  const cardClosedBeforePreviewNavigation = await ctx.session.eval('document.querySelector(".lanes-card")?.hidden === true');
  await openList(ctx, 103);
  await ctx.session.click(`.next-question-heading-link[data-testid="next-question-heading-${previewQ.question_id}"]`);
  await wait(ctx, `document.querySelector('.next-lanes-scroll')?.dataset.level === 'spec' && document.querySelector('.next-lanes-spec-title')?.textContent === ${sel(previewQuestionMember.title)}
    && !document.querySelector('.lanes-card').hidden`, 'preview question opens the visible lanes card at its spec');
  const previewCardOk = await ctx.session.eval(`(() => {const card=document.querySelector('.next-lanes-scroll .next-question-card[data-question-id="${previewQ.question_id}"]');return !!card?.querySelector('.next-question-form')
    && !card.querySelector('.next-question-skip,[aria-label*="Voice"],button[class*="voice"]')})()`);
  check(ctx, 'previewed uncapped question card opens visibly with a form and no skip or voice control', cardClosedBeforePreviewNavigation && previewCardOk
    && await ctx.session.eval(`!document.querySelector('.lanes-card').hidden && document.querySelector('.next-lanes-scroll').dataset.level==='spec'`));
  await ctx.session.click('[aria-label^="Questions"]');
  await wait(ctx, "!!document.querySelector('.next-questions-list')", 'question list reopens');
  await ctx.session.click('.next-lanes-close');
  const cardClosedBeforeOffPreviewNavigation = await ctx.session.eval('document.querySelector(".lanes-card")?.hidden === true');
  await openList(ctx, 103);
  await ctx.session.click(`.next-question-heading-link[data-testid="next-question-heading-${offQ.question_id}"]`);
  await wait(ctx, `document.querySelector('.next-lanes-scroll')?.dataset.level === 'spec' && document.querySelector('.next-lanes-spec-title')?.textContent === ${sel(offPreviewQuestionMember.title)}
    && !document.querySelector('.lanes-card').hidden`, 'off-preview question opens the visible lanes card at its spec');
  const offCardOk = await ctx.session.eval(`(() => {const card=document.querySelector('.next-lanes-scroll .next-question-card[data-question-id="${offQ.question_id}"]');return !!card?.querySelector('.next-question-form')
    && !card.querySelector('.next-question-skip,[aria-label*="Voice"],button[class*="voice"]')})()`);
  check(ctx, 'off-preview question card opens visibly with a form and no skip or voice control', cardClosedBeforeOffPreviewNavigation && offCardOk
    && await ctx.session.eval(`!document.querySelector('.lanes-card').hidden && document.querySelector('.next-lanes-scroll').dataset.level==='spec'`));
  await ctx.session.click('[aria-label^="Questions"]');
  await wait(ctx, "!!document.querySelector('.next-questions-list')", 'question list reopens');
  await ctx.session.click('.next-lanes-close');
  const chatRow = `.next-question-list-item[data-question-id="${chatQ.question_id}"] .next-question-chat-link`;
  await ctx.session.click(chatRow);
  await wait(ctx, "!document.querySelector('.card').hidden && document.querySelector('.card .thread-inner').dataset.streamId==='local:web-gate-1'", 'listed chat heading opens its chat');
  check(ctx, 'no-spec listed-chat heading opens that chat', true);
  await openList(ctx, 103);
  check(ctx, 'no-spec unlisted chat and orphan spec never link to the chat', await ctx.session.eval(`(() => {
    const a=document.querySelector('.next-question-list-item[data-question-id="${unlistedQ.question_id}"] .next-question-list-heading');
    const b=document.querySelector('.next-question-list-item[data-question-id="${orphanQ.question_id}"] .next-question-list-heading');
    return !!a?.querySelector('.next-question-heading-title') && !a.querySelector('button')
      && !!b?.querySelector('.next-question-heading-title') && !b.querySelector('button');
  })()`));
}

async function nextQuestionAnswerRun(ctx, ownedIds, hooks) {
  const firstSpec = 'spec_gate__next_lanes_ordinary_a_00';
  const secondSpec = 'spec_gate__next_lanes_ordinary_a_01';
  const choices = [{ label: 'Keep fixture value', value: 'keep' }, { label: 'Change fixture value', value: 'change' }];
  const first = await H.seedQuestion(ctx, ownedIds, { key: 'e4-first', specId: firstSpec, title: 'Synthetic first answer', options: choices, allowCustom: true });
  const second = await H.seedQuestion(ctx, ownedIds, { key: 'e4-second', specId: secondSpec, title: 'Synthetic second answer', options: choices });
  let lane = F.makeOrdinaryLane({ laneId: F.ORDINARY_A_ID, title: 'Fixture answer lane', goalTitle: 'Fixture answer goal', memberCount: 4,
    questionSpecIds: [firstSpec, secondSpec], questionIdsBySpec: { [firstSpec]: [first.question_id], [secondSpec]: [second.question_id] } });
  await install(ctx, hooks, 1440, [lane], F.inventoryFrame([lane]));
  await wait(ctx, `document.querySelector('[aria-label="Questions (2)"]')`, 'two open questions');
  await ctx.session.click(rowSelector(lane.inventory.lane_id));
  await wait(ctx, 'document.querySelector(".next-lanes-scroll")?.dataset.level === "lane"', 'answer lane opens');
  await wait(ctx, 'document.querySelectorAll(".next-question-card").length === 1', 'question card mounted in lane');
  const initial = await ctx.session.eval(`(() => ({id:document.querySelector('.next-question-card')?.dataset.questionId,
    label:document.querySelector('.next-question-card-label')?.textContent,previous:!!document.querySelector('.next-question-prev'),next:!!document.querySelector('.next-question-next'),
    about:document.querySelector('.next-question-card .next-question-about')?.textContent||'',
    beforeStops:!!document.querySelector('.next-question-cards')?.nextElementSibling?.classList.contains('next-lanes-stops')}))()`);
  const initialMemberSpec = initial.id === first.question_id ? firstSpec : secondSpec;
  const initialMemberTitle = lane.members.find(member=>member.spec_id===initialMemberSpec)?.title;
  check(ctx, 'two lane questions share one paged card with an observed starting question', initial.label === 'Question 1 of 2'
    && [first.question_id, second.question_id].includes(initial.id) && initial.previous && initial.next
    && initial.about === `Fixture answer goal · ${initialMemberTitle}` && initial.beforeStops, { ...initial, expectedAbout:`Fixture answer goal · ${initialMemberTitle}` });
  const otherId = initial.id === first.question_id ? second.question_id : first.question_id;
  await ctx.session.click('.next-question-next');
  await wait(ctx, `!!document.querySelector('.next-question-card[data-question-id="${otherId}"]')`, 'question pager reaches the other answer');
  await ctx.session.click('.next-question-prev');
  await wait(ctx, `!!document.querySelector('.next-question-card[data-question-id="${initial.id}"]')`, 'question pager moves backward');
  await ctx.session.click('.next-question-prev');
  await wait(ctx, `!!document.querySelector('.next-question-card[data-question-id="${otherId}"]')`, 'question pager wraps backward');
  await ctx.session.click('.next-question-next');
  await wait(ctx, `!!document.querySelector('.next-question-card[data-question-id="${initial.id}"]')`, 'question pager wraps forward');
  if (initial.id !== first.question_id) {
    await ctx.session.click('.next-question-next');
    await wait(ctx, `!!document.querySelector('.next-question-card[data-question-id="${first.question_id}"]')`, 'question pager selects the custom-answer question');
  }

  const card = '.next-question-card';
  await ctx.session.eval(`(() => { const probe={sentAt:null,nextAt:null}; window.__nextQuestionReceiptTiming=probe;
    const observer=new MutationObserver(()=>{
      if (probe.sentAt===null && [...document.querySelectorAll('.next-question-card[data-question-id="${first.question_id}"] .next-question-sent-label')].some(node=>node.textContent.trim()==='Answer sent')) probe.sentAt=performance.now();
      if (probe.sentAt!==null && probe.nextAt===null && document.querySelector('.next-question-card[data-question-id="${second.question_id}"]')) probe.nextAt=performance.now();
    }); observer.observe(document.documentElement,{subtree:true,childList:true,characterData:true}); probe.observer=observer; return true; })()`);
  await realPointerClick(ctx, `${card} .next-question-option:nth-child(1)`);
  const preset = await ctx.session.eval(`({selected:document.querySelector('${card} .next-question-option:nth-child(1)')?.classList.contains('is-selected'),enabled:!document.querySelector('${card} .next-question-send')?.disabled})`);
  const typed = 'Synthetic typed answer';
  await realPointerClick(ctx, `${card} .next-question-own`);
  await ctx.session.send('Input.insertText', { text: typed });
  const typedState = await ctx.session.eval(`({selected:document.querySelector('${card} .next-question-option.is-selected')?.textContent.trim() || '',
    active:document.activeElement===document.querySelector('${card} .next-question-own'),
    text:document.querySelector('${card} .next-question-own')?.value,dimmed:document.querySelector('${card} .next-question-own')?.classList.contains('is-dimmed')})`);
  await realPointerClick(ctx, `${card} .next-question-option:nth-child(2)`);
  const secondPreset = await ctx.session.eval(`({selected:document.querySelector('${card} .next-question-option:nth-child(2)')?.classList.contains('is-selected'),
    active:document.activeElement===document.querySelector('${card} .next-question-option:nth-child(2)'),
    dimmed:document.querySelector('${card} .next-question-own')?.classList.contains('is-dimmed')})`);
  await openList(ctx, 2);
  const listPreset = await ctx.session.eval(`(() => {const row=document.querySelector('.next-question-list-item[data-question-id="${first.question_id}"]');return {
    selected:!!row?.querySelector('.next-question-option:nth-child(2).is-selected'),dimmed:row?.querySelector('.next-question-own')?.classList.contains('is-dimmed')||false,
    text:row?.querySelector('.next-question-own')?.value||''};})()`);
  await ctx.session.click('[aria-label^="Questions"]');
  await realPointerClick(ctx, `${card} .next-question-own`);
  const focusedText = await ctx.session.eval(`({active:document.activeElement===document.querySelector('${card} .next-question-own'),
    selected:document.querySelector('${card} .next-question-option.is-selected')?.textContent.trim() || '',
    dimmed:document.querySelector('${card} .next-question-own')?.classList.contains('is-dimmed'),text:document.querySelector('${card} .next-question-own')?.value})`);
  await ctx.session.click(`${card} .next-question-next`);
  await wait(ctx, `!!document.querySelector('.next-question-card[data-question-id="${second.question_id}"]')`, 'typed draft pages to the other question');
  await ctx.session.click(`${card} .next-question-prev`);
  await wait(ctx, `!!document.querySelector('.next-question-card[data-question-id="${first.question_id}"]')`, 'typed draft pages back to its question');
  const pagedDraft = await ctx.session.eval(`(() => ({text:document.querySelector('${card} .next-question-own')?.value||'',
    selected:!!document.querySelector('${card} .next-question-option.is-selected'),dimmed:document.querySelector('${card} .next-question-own')?.classList.contains('is-dimmed')||false}))()`);
  check(ctx, 'preset then typed then preset then focus typed preserves the intended draft', preset.selected && preset.enabled
    && !typedState.selected && typedState.active && typedState.text === typed && typedState.dimmed === false
    && secondPreset.selected && secondPreset.active && secondPreset.dimmed
    && listPreset.selected && listPreset.dimmed && listPreset.text === typed
    && focusedText.active && !focusedText.selected && focusedText.dimmed === false && focusedText.text === typed
    && pagedDraft.text === typed && !pagedDraft.selected && !pagedDraft.dimmed,
  { preset, typedState, secondPreset, listPreset, focusedText, pagedDraft });
  await openList(ctx, 2);
  const listTyped = await ctx.session.eval(`(() => {const row=document.querySelector('.next-question-list-item[data-question-id="${first.question_id}"]');return {
    text:row?.querySelector('.next-question-own')?.value||'',selected:!!row?.querySelector('.next-question-option.is-selected'),
    dimmed:row?.querySelector('.next-question-own')?.classList.contains('is-dimmed')||false};})()`);
  check(ctx, 'focused typed mode and text are shared with the questions list', listTyped.text === typed && !listTyped.selected && !listTyped.dimmed, listTyped);
  await ctx.session.click('[aria-label^="Questions"]');
  await ctx.session.click('.next-lanes-close');
  await wait(ctx, `document.querySelector('.lanes-card')?.hidden === true`, 'lanes card closes with the typed answer draft');
  await ctx.session.click('[aria-label="Lanes"]');
  await wait(ctx, `document.querySelector('.lanes-card')?.hidden === false`, 'lanes card reopens after preserving the typed draft');
  await ctx.session.click(rowSelector(lane.inventory.lane_id));
  await wait(ctx, `document.querySelector('.next-lanes-scroll')?.dataset.level === 'lane'`, 'answer lane reopens');
  await wait(ctx, `!!document.querySelector('.next-question-card[data-question-id="${first.question_id}"]')
    || !!document.querySelector('.next-question-card[data-question-id="${second.question_id}"]')`, 'a lane question card remounts after close');
  const remountedCardId = await ctx.session.eval(`document.querySelector('.next-question-card')?.dataset.questionId || null`);
  if (remountedCardId !== first.question_id) {
    await ctx.session.click('.next-question-prev');
    await wait(ctx, `!!document.querySelector('.next-question-card[data-question-id="${first.question_id}"]')`, 'remounted pager returns to typed question');
  }
  await wait(ctx, `document.querySelector('.next-question-card[data-question-id="${first.question_id}"] .next-question-own')?.value === ${sel(typed)}`,
    'typed answer draft survives closing and reopening the lanes card');
  const remountedDraft = await ctx.session.eval(`(() => {const root=document.querySelector('.next-question-card[data-question-id="${first.question_id}"]');return {
    visible:!!root,text:root?.querySelector('.next-question-own')?.value||'',selected:!!root?.querySelector('.next-question-option.is-selected'),
    dimmed:root?.querySelector('.next-question-own')?.classList.contains('is-dimmed')||false};})()`);
  check(ctx, 'typed answer draft survives closing and reopening the lanes card', remountedDraft.visible
    && remountedDraft.text === typed && !remountedDraft.selected && !remountedDraft.dimmed, remountedDraft);
  await ctx.session.click(`${card} .next-question-send`);
  await wait(ctx, `document.querySelector(${sel(`${card} .next-question-sent-label`)})?.textContent === 'Answer sent'
    && document.querySelector(${sel(`${card} .next-question-sent-answer`)})?.textContent === ${sel(typed)}`, 'exact answer receipt');
  const receipt = await ctx.session.eval(`(() => ({text:document.querySelector('${card} .next-question-sent-answer')?.textContent || '',
    label:document.querySelector('${card} .next-question-sent-label')?.textContent || '',
    arrows:document.querySelectorAll('${card} .next-question-prev,${card} .next-question-next').length,
    count:document.querySelector('[aria-label^="Questions"]')?.dataset.count}))()`);
  check(ctx, 'send shows the exact Answer sent receipt, removes page arrows, and drops the count immediately',
    receipt.label === 'Answer sent' && receipt.text === typed && receipt.arrows === 0 && receipt.count === '1', receipt);
  const stored = await fixtureRequest(ctx.runtime, H.PRODUCER, { type: 'prompt.status', question_id: first.question_id });
  check(ctx, 'isolated daemon stores exactly the typed answer', stored?.ok === true && ['answered', 'consumed'].includes(stored.question?.state)
    && stored.question?.answer?.custom_text === typed, { state: stored?.question?.state, answer: stored?.question?.answer });
  const receiptTiming = await ctx.session.eval(`(() => {const p=window.__nextQuestionReceiptTiming;return p?{sentAt:p.sentAt,nextAt:p.nextAt,elapsed:p.sentAt!==null&&p.nextAt!==null?p.nextAt-p.sentAt:null}:null})()`);
  await wait(ctx, `!!document.querySelector('.next-question-card[data-question-id="${second.question_id}"]')`, 'card advances after receipt window');
  const finalReceiptTiming = await ctx.session.eval(`(() => {const p=window.__nextQuestionReceiptTiming;if(p?.observer)p.observer.disconnect();return p?{sentAt:p.sentAt,nextAt:p.nextAt,elapsed:p.sentAt!==null&&p.nextAt!==null?p.nextAt-p.sentAt:null}:null})()`);
  check(ctx, 'the Answer sent receipt remains visible for about 2.6 seconds before advancing',
    finalReceiptTiming?.elapsed >= 2300 && finalReceiptTiming?.elapsed <= 4000, finalReceiptTiming || receiptTiming);
  check(ctx, 'card advances to the other unanswered lane question', await ctx.session.eval(`document.querySelector('.next-question-card')?.dataset.questionId === ${sel(second.question_id)}`));

  const openQuestionLaneCard = await ctx.session.eval(`(() => {const card=document.querySelector('.next-question-card[data-question-id="${second.question_id}"]');return !!card?.classList.contains('is-attention') && !!card.querySelector('.next-question-form')})()`);
  check(ctx, 'the still-open question keeps its tinted answer card in the lane', openQuestionLaneCard);
  await ctx.session.click(`.next-lanes-stop-open[data-goal-id="${lane.goals[0].goal_id}"]`);
  await wait(ctx, 'document.querySelector(".next-lanes-scroll")?.dataset.level === "goal"', 'answered question goal view');
  const secondTitle = lane.members.find(member=>member.spec_id===secondSpec)?.title;
  const openAtGoal = await ctx.session.eval(`(() => {const card=document.querySelector('.next-question-card[data-question-id="${second.question_id}"]');
    const needs=document.querySelector('.next-lanes-section-needs .next-lanes-member-title[data-spec-id="${secondSpec}"]');
    return {card:!!card?.classList.contains('is-attention'),member:!!needs,about:card?.querySelector('.next-question-about')?.textContent||'',
      beforeNeeds:!!card&&!!needs&&!!(card.compareDocumentPosition(needs)&Node.DOCUMENT_POSITION_FOLLOWING)};})()`);
  check(ctx, 'the open question card sits before its Needs member with a tinted card and exact about-spec label',
    openAtGoal.card && openAtGoal.member && openAtGoal.about === secondTitle && openAtGoal.beforeNeeds,
    { ...openAtGoal, secondTitle });
  await ctx.session.click(`.next-lanes-member-title[data-spec-id="${secondSpec}"]`);
  await wait(ctx, 'document.querySelector(".next-lanes-scroll")?.dataset.level === "spec"', 'open question spec view');
  const openAtSpec = await ctx.session.eval(`(() => ({card:document.querySelector('.next-question-card[data-question-id="${second.question_id}"]')?.classList.contains('is-attention') || false,
    form:!!document.querySelector('.next-question-card[data-question-id="${second.question_id}"] .next-question-form')}))()`);
  check(ctx, 'the open question is shown in a tinted answer card at spec level', openAtSpec.card && openAtSpec.form, openAtSpec);
  await ctx.session.click('.next-lanes-back');
  await wait(ctx, 'document.querySelector(".next-lanes-scroll")?.dataset.level === "goal"', 'return to answered question goal');
  await ctx.session.click(`.next-lanes-member-title[data-spec-id="${firstSpec}"]`);
  await wait(ctx, `document.querySelector('.next-lanes-you-said')?.textContent.includes(${sel(typed)})`, 'You said loads before reload');
  check(ctx, 'answered spec shows You said before reload', await ctx.session.eval(`document.querySelector('.next-lanes-you-said')?.textContent.includes(${sel(typed)})`));

  // Reopen the app against the same isolated daemon to prove You said hydration.
  lane = F.makeOrdinaryLane({ laneId: F.ORDINARY_A_ID, title: 'Fixture answer lane', goalTitle: 'Fixture answer goal', memberCount: 4,
    questionSpecIds: [firstSpec, secondSpec], questionIdsBySpec: { [secondSpec]: [second.question_id] } });
  await H.cleanupHook(ctx, hooks.pop());
  await install(ctx, hooks, 1440, [lane], F.inventoryFrame([lane]));
  await ctx.session.click(rowSelector(lane.inventory.lane_id));
  await wait(ctx, `document.querySelector('.next-lanes-scroll')?.dataset.level === 'lane'`, 'lane opens after reload');
  await ctx.session.click(`.next-lanes-stop-open[data-goal-id="${lane.goals[0].goal_id}"]`);
  await wait(ctx, 'document.querySelector(".next-lanes-scroll")?.dataset.level === "goal"', 'goal opens after reload');
  await ctx.session.click(`.next-lanes-member-title[data-spec-id="${firstSpec}"]`);
  await wait(ctx, `document.querySelector('.next-lanes-you-said')?.textContent.includes(${sel(typed)})`, 'You said hydrates after reload');
  check(ctx, 'answered spec retains You said after reload', await ctx.session.eval(`document.querySelector('.next-lanes-you-said')?.textContent.includes(${sel(typed)})`));
}

async function nextQuestionUncertainRun(ctx, ownedIds, hooks) {
  const choices = [{ label: 'Use fixture value', value: 'use' }, { label: 'Keep fixture value', value: 'keep' }];
  const before = await H.seedQuestion(ctx, ownedIds, { key: 'e5-before', title: 'Synthetic retry answer', options: choices });
  const lost = await H.seedQuestion(ctx, ownedIds, { key: 'e5-lost', title: 'Synthetic uncertain answer', options: choices });
  const frame = F.inventoryFrame([]);
  await install(ctx, hooks, 1440, [], frame);
  await wait(ctx, `document.querySelector('[aria-label="Questions (2)"]')`, 'uncertain-answer questions');
  await openList(ctx, 2);
  const beforeNotification = await H.notificationFor(ctx, before.question_id);
  const beforeNotificationId = beforeNotification?.notification_id || beforeNotification?.id || null;
  const lostNotification = await H.notificationFor(ctx, lost.question_id);
  const lostNotificationId = lostNotification?.notification_id || lostNotification?.id || null;
  check(ctx, 'both durable open questions have notification identities before send', !!beforeNotificationId && !!lostNotificationId,
    { beforeNotificationId, lostNotificationId });
  await ctx.session.eval(`(() => {
    const cc=window.cc; const original=cc.notificationResolve; const gate={calls:[],timeline:[],mode:'before',original,
      questionIds:${json({ [beforeNotificationId]: before.question_id, [lostNotificationId]: lost.question_id })}};
    cc.notificationResolve=async (...args)=>{
      const entry={type:'notification.resolve:start',sequence:gate.calls.length+1,notificationId:args[0]||null,
        questionId:gate.questionIds[args[0]]||args[2]?.question_id||args[2]?.answer?.question_id||null,
        mode:gate.mode,status:'started',at:performance.now()};
      gate.calls.push(args);gate.timeline.push(entry);
      if(gate.mode==='before'){entry.status='failed_before_daemon';gate.timeline.push({type:'notification.resolve:failed',sequence:entry.sequence,
        questionId:entry.questionId,status:entry.status,at:performance.now()});throw new Error('synthetic pre-send failure');}
      const reply=await original(...args);entry.status='committed';entry.atResolved=performance.now();
      if(gate.mode==='lost'){gate.mode='normal';entry.status='committed_reply_lost';gate.timeline.push({type:'notification.resolve:reply_lost',sequence:entry.sequence,
        questionId:entry.questionId,status:entry.status,at:performance.now()});throw new Error('synthetic lost reply');}
      gate.timeline.push({type:'notification.resolve:reply',sequence:entry.sequence,questionId:entry.questionId,status:entry.status,at:performance.now()});
      return reply;
    };
    window.__nextQuestionResolveGate=gate; return true;
  })()`);
  const firstRow = `.next-question-list-item[data-question-id="${before.question_id}"]`;
  const beforeReadSequence = await ctx.session.eval('window.__nextLanesPromptReads.length');
  await ctx.session.click(`${firstRow} .next-question-option`);
  await ctx.session.click(`${firstRow} .next-question-send`);
  await wait(ctx, `document.querySelector(${sel(firstRow)})?.textContent.includes('Not sent')`, 'pre-send uncertainty reported');
  await wait(ctx, `window.__nextLanesPromptReads.some(row=>row.sequence>${beforeReadSequence}
    &&row.args?.producer_stream_id===${sel(H.PRODUCER)}&&row.args?.open===false&&row.status==='resolved'
    &&row.questionIds.filter(id=>id===${sel(before.question_id)}).length===1)`, 'pre-send prompt.list reconciliation');
  const notSent = await ctx.session.eval(`({selected:!!document.querySelector(${sel(firstRow)}+' .next-question-option.is-selected'),
    enabled:!document.querySelector(${sel(firstRow)}+' .next-question-send')?.disabled,
    count:document.querySelector('[aria-label^="Questions"]')?.dataset.count})`);
  check(ctx, 'pre-send failure keeps the selected choice, shows Not sent, and keeps the question open', notSent.selected && notSent.enabled && notSent.count === '2', notSent);
  const beforeStatus = await fixtureRequest(ctx.runtime, H.PRODUCER, { type: 'prompt.status', question_id: before.question_id });
  const beforeCalls = await ctx.session.eval('window.__nextQuestionResolveGate.calls.length');
  const beforeEvidence = await ctx.session.eval(`(() => {
    const reads=window.__nextLanesPromptReads.filter(row=>row.sequence>${beforeReadSequence}&&row.args?.producer_stream_id===${sel(H.PRODUCER)}&&row.args?.open===false);
    const timeline=window.__nextQuestionResolveGate.timeline;
    const resolve=timeline.find(row=>row.type==='notification.resolve:start'&&row.questionId===${sel(before.question_id)});
    const failed=timeline.find(row=>row.type==='notification.resolve:failed'&&row.questionId===${sel(before.question_id)});
    const reconcileRead=reads.find(row=>row.records.some(record=>record.question_id===${sel(before.question_id)}));
    const promptTimeline=reconcileRead?timeline.filter(row=>row.sequence===reconcileRead.sequence&&row.type.startsWith('prompt.list:')):[];
    return {reads,resolve,failed,reconcileRead,
      reconcileRecord:reconcileRead?.records.find(record=>record.question_id===${sel(before.question_id)})||null,
      promptTimeline,
      timeline:timeline.filter(row=>row.questionId===${sel(before.question_id)})};
  })()`);
  check(ctx, 'pre-send failure leaves the durable question open with no stored answer after one failed resolve',
    beforeStatus?.ok === true && beforeStatus.question?.question_id === before.question_id
      && beforeStatus.question?.state === 'open' && !beforeStatus.question?.answer && beforeCalls === 1
      && beforeEvidence.resolve?.notificationId === beforeNotificationId && beforeEvidence.resolve?.mode === 'before'
      && beforeEvidence.resolve?.status === 'failed_before_daemon'
      && beforeEvidence.failed?.status === 'failed_before_daemon'
      && beforeEvidence.reconcileRead?.status === 'resolved'
      && beforeEvidence.reconcileRead?.questionIds.filter(id=>id===before.question_id).length === 1
      && beforeEvidence.promptTimeline.length === 2
      && beforeEvidence.promptTimeline[0]?.type === 'prompt.list:start'
      && beforeEvidence.promptTimeline[1]?.type === 'prompt.list:resolved'
      && beforeEvidence.promptTimeline.every(row=>row.sequence===beforeEvidence.reconcileRead.sequence)
      && beforeEvidence.failed.at < beforeEvidence.promptTimeline[0]?.at
      && beforeEvidence.reconcileRecord?.state === 'open' && !beforeEvidence.reconcileRecord?.hasStoredAnswer,
    { state: beforeStatus?.question?.state, answer: beforeStatus?.question?.answer || null, beforeCalls, beforeNotificationId, beforeEvidence });
  await ctx.session.eval("window.__nextQuestionResolveGate.mode='normal'");
  await ctx.session.click(`${firstRow} .next-question-send`);
  await wait(ctx, `document.querySelector(${sel(firstRow)})?.textContent.includes('Answer sent')`, 'retry send succeeds');
  const afterRetry = await fixtureRequest(ctx.runtime, H.PRODUCER, { type: 'prompt.status', question_id: before.question_id });
  const retryEntries = await ctx.session.eval(`window.__nextQuestionResolveGate.timeline.filter(row=>row.type==='notification.resolve:start'&&row.questionId===${sel(before.question_id)})`);
  check(ctx, 'retry succeeds against the gate daemon once', afterRetry?.question?.question_id === before.question_id
    && ['answered', 'consumed'].includes(afterRetry?.question?.state)
    && afterRetry?.question?.answer?.selections?.length === 1 && afterRetry?.question?.answer?.selections?.[0] === 'use'
    && retryEntries.length === 2 && retryEntries.every(row=>row.notificationId===beforeNotificationId),
  { state: afterRetry?.question?.state, answer: afterRetry?.question?.answer, retryEntries });

  const lostRow = `.next-question-list-item[data-question-id="${lost.question_id}"]`;
  const lostBefore = await fixtureRequest(ctx.runtime, H.PRODUCER, { type: 'prompt.status', question_id: lost.question_id });
  const lostReadSequence = await ctx.session.eval('window.__nextLanesPromptReads.length');
  await ctx.session.click(`${lostRow} .next-question-option:nth-child(2)`);
  await ctx.session.eval("window.__nextQuestionResolveGate.mode='lost'");
  await ctx.session.click(`${lostRow} .next-question-send`);
  await wait(ctx, `document.querySelector(${sel(lostRow)})?.textContent.includes('Answer sent')`, 'lost reply reconciles');
  const lostStatus = await fixtureRequest(ctx.runtime, H.PRODUCER, { type: 'prompt.status', question_id: lost.question_id });
  const reconciliation = await ctx.session.eval(`(() => {
    const reads=window.__nextLanesPromptReads.filter(row=>row.sequence>${lostReadSequence}&&row.args?.producer_stream_id===${sel(H.PRODUCER)}&&row.args?.open===false);
    const resolve=window.__nextQuestionResolveGate.timeline.find(row=>row.type==='notification.resolve:start'&&row.questionId===${sel(lost.question_id)});
    const matching=reads.flatMap(read=>read.records.filter(record=>record.question_id===${sel(lost.question_id)}).map(record=>({read,record})));
    const finished=window.__nextQuestionResolveGate.timeline.find(row=>row.type==='notification.resolve:reply_lost'&&row.questionId===${sel(lost.question_id)});
    const promptTimeline=reads.length?window.__nextQuestionResolveGate.timeline.filter(row=>row.sequence===reads[0].sequence&&row.type.startsWith('prompt.list:')):[];
    return {reads,resolve,finished,matching,promptTimeline,timeline:window.__nextQuestionResolveGate.timeline.filter(row=>row.questionId===${sel(lost.question_id)})};
  })()`);
  const lostResolveCount = await ctx.session.eval(`window.__nextQuestionResolveGate.timeline.filter(row=>row.type==='notification.resolve:start'&&row.questionId===${sel(lost.question_id)}).length`);
  const lostReceipt = await ctx.session.eval(`(() => ({label:document.querySelector(${sel(`${lostRow} .next-question-sent-label`)})?.textContent || '',
    answer:document.querySelector(${sel(`${lostRow} .next-question-sent-answer`)})?.textContent || ''}))()`);
  const reconcileRead = reconciliation.matching[0];
  const reconciledRead = !!reconcileRead && reconciliation.reads.length === 1 && reconcileRead.record.state !== 'open'
    && reconcileRead.record.hasStoredAnswer && JSON.stringify(reconcileRead.record.selections) === '["keep"]'
    && reconciliation.reads[0].status === 'resolved' && reconciliation.reads[0].questionIds.filter(id=>id===lost.question_id).length === 1;
  check(ctx, 'lost success reply reconciles through prompt.list to the exact stored answer and receipt',
    lostStatus?.question?.question_id === lost.question_id && ['answered', 'consumed'].includes(lostStatus?.question?.state)
      && lostStatus.question?.answer?.selections?.length === 1 && lostStatus.question?.answer?.selections?.[0] === 'keep'
      && lostBefore?.question?.question_id === lost.question_id && lostBefore.question.state === 'open' && !lostBefore.question.answer
      && reconciliation.resolve?.notificationId === lostNotificationId && reconciliation.finished?.status === 'committed_reply_lost'
      && reconciliation.resolve.at < reconciliation.finished.at
      && reconciliation.finished.at < reconciliation.promptTimeline[0]?.at
      && reconciliation.promptTimeline[0]?.type === 'prompt.list:start'
      && reconciliation.promptTimeline.at(-1)?.type === 'prompt.list:resolved'
      && lostResolveCount === 1 && reconciledRead
      && lostReceipt.label === 'Answer sent' && lostReceipt.answer === 'Keep fixture value',
    { state: lostStatus?.question?.state, answer: lostStatus?.question?.answer, before: lostBefore?.question,
      lostNotificationId, resolveCount: lostResolveCount, reconciliation, reconciledRead, lostReceipt });
}

module.exports = {
  nextLanesRoot: isolated(nextLanesRootRun),
  nextLanesDrill: isolated(nextLanesDrillRun),
  nextQuestionsList: isolated(nextQuestionsListRun),
  nextQuestionAnswer: isolated(nextQuestionAnswerRun),
  nextQuestionUncertain: isolated(nextQuestionUncertainRun),
};
