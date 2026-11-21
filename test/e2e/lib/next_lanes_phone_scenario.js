'use strict';

const H = require('./next_lanes_harness');
const F = require('./next_lanes_fixtures');

const PHONE_WIDTH = 440;
const PHONE_HEIGHT = 956;
const REVISION = F.REVISION_A;

function check(ctx, label, condition, evidence = {}) {
  const passed = condition === true;
  ctx.report.ok(label, passed, evidence);
  if (!passed) throw new Error(`assertion failed: ${label}`);
}

function errorDetail(error) {
  const detail = { name: error?.name || 'Error', message: String(error?.message || error) };
  if (error?.stack) detail.stack = String(error.stack);
  if (error?.cleanupError) detail.cleanupError = typeof error.cleanupError === 'object'
    ? { ...error.cleanupError }
    : String(error.cleanupError);
  if (Array.isArray(error?.cleanupErrors)) detail.cleanupErrors = error.cleanupErrors.map(errorDetail);
  if (error?.cause) detail.cause = typeof error.cause === 'object' ? errorDetail(error.cause) : String(error.cause);
  if (Array.isArray(error?.errors)) detail.errors = error.errors.map(errorDetail);
  return detail;
}

async function wait(ctx, expression, label) {
  await ctx.session.waitFor(expression, { timeoutMs: ctx.timeoutMs, label });
}

function rowSelector(laneId) {
  return `.next-lanes-row-open[data-lane-id=${JSON.stringify(laneId)}]`;
}

function readExpression(laneId, revision, goalId = null) {
  const goalMatch = goalId === null
    ? '!(call.options.goalId || null)'
    : `call.options.goalId === ${JSON.stringify(goalId)}`;
  return `window.__nextLanes.calls.some(call => call.laneId === ${JSON.stringify(laneId)}
    && call.revision === ${JSON.stringify(revision)}
    && Number(call.options.membersLimit) === 32
    && ${goalMatch}
    && call.status === 'resolved' && call.answeredRevision === ${JSON.stringify(revision)})`;
}

async function waitForQuestionCard(ctx, acceptedQuestionIds, expectedCount) {
  await wait(ctx, `(() => {
    const cards=[...document.querySelectorAll('.next-lanes-scroll .next-question-card')];
    const card=cards[0];
    const allowed=${JSON.stringify(acceptedQuestionIds)};
    return cards.length === 1 && allowed.includes(card?.dataset.questionId)
      && (!!card.querySelector('.next-question-form') || !!card.querySelector('.next-question-sent'))
      && (card.querySelectorAll('.next-question-page').length === (${expectedCount} > 1 ? 2 : 0));
  })()`, `a ready ${expectedCount}-question card is rendered in the lanes route`);
}

async function selectQuestionCard(ctx, questionId, expectedCount) {
  for (let attempt = 0; attempt < expectedCount + 1; attempt += 1) {
    const current = await ctx.session.eval("document.querySelector('.next-lanes-scroll .next-question-card')?.dataset.questionId || null");
    if (current === questionId) return current;
    const clicked = await ctx.session.eval(`(() => {
      const next=document.querySelector('.next-lanes-scroll .next-question-next');
      if (!next) return false;
      next.click();
      return true;
    })()`);
    if (!clicked) break;
    await wait(ctx, `document.querySelector('.next-lanes-scroll .next-question-card')?.dataset.questionId !== ${JSON.stringify(current)}`,
      'question pager advances before layout measurement');
  }
  const selected = await ctx.session.eval("document.querySelector('.next-lanes-scroll .next-question-card')?.dataset.questionId || null");
  if (selected !== questionId) throw new Error(`phone question pager selected ${selected}, expected ${questionId}`);
  return selected;
}

async function phoneGeometry(ctx) {
  return ctx.session.eval(`(() => {
    const rect = node => {
      if (!node) return null;
      const r=node.getBoundingClientRect();
      return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height};
    };
    const app=document.querySelector('.next-app');
    const stage=document.querySelector('.stage');
    const rail=document.querySelector('.rail');
    const assistant=stage?.querySelector('.pane');
    const card=document.querySelector('.lanes-card');
    const lanesScroll=card?.querySelector('.next-lanes-scroll');
    const popover=document.querySelector('.questions-popover:not([hidden])');
    const questionList=popover?.classList.contains('next-questions-list') ? popover : null;
    const dimensions=node => node ? {
      rect:rect(node),clientWidth:node.clientWidth,scrollWidth:node.scrollWidth,
      clientHeight:node.clientHeight,scrollHeight:node.scrollHeight,
      overflowX:getComputedStyle(node).overflowX,overflowY:getComputedStyle(node).overflowY,
    } : null;
    return {
      viewport:{width:innerWidth,height:innerHeight},
      document:{clientWidth:document.documentElement.clientWidth,scrollWidth:document.documentElement.scrollWidth,
        bodyClientWidth:document.body.clientWidth,bodyScrollWidth:document.body.scrollWidth,scrollX},
      app:rect(app),stage:rect(stage),rail:{hidden:rail?.hidden,rect:rect(rail)},
      assistant:{hidden:assistant?.hidden,rect:rect(assistant)},
      card:{hidden:card?.hidden,alone:card?.classList.contains('alone'),rect:rect(card)},
      lanesScroll:dimensions(lanesScroll),
      popover:dimensions(popover),questionList:dimensions(questionList),questionListSameNode:questionList === popover,
      railQuestionCount:document.querySelector('[aria-label^="Questions"]')?.dataset.count || null,
    };
  })()`);
}

function near(a, b) {
  return Number.isFinite(a) && Math.abs(a - b) <= 1;
}

function assertFullPhoneStage(ctx, label, geometry) {
  const stage = geometry.stage;
  const rail = geometry.rail?.rect;
  const card = geometry.card?.rect;
  const scroll = geometry.lanesScroll;
  const passed = geometry.viewport?.width === PHONE_WIDTH && geometry.viewport?.height === PHONE_HEIGHT
    && geometry.document?.clientWidth === PHONE_WIDTH && geometry.document?.scrollWidth === PHONE_WIDTH
    && geometry.document?.bodyClientWidth === PHONE_WIDTH && geometry.document?.bodyScrollWidth === PHONE_WIDTH
    && geometry.document?.scrollX === 0
    && near(rail?.left, 0) && near(rail?.top, 0) && near(rail?.width, PHONE_WIDTH) && near(rail?.height, 52)
    && near(stage?.left, 0) && near(stage?.top, 52) && near(stage?.width, PHONE_WIDTH)
    && near(stage?.height, PHONE_HEIGHT - 52) && near(stage?.bottom, PHONE_HEIGHT)
    && geometry.assistant?.hidden === true
    && geometry.card?.hidden === false && geometry.card?.alone === true
    && geometry.questionListSameNode === true
    && near(card?.left, stage?.left) && near(card?.top, stage?.top)
    && near(card?.right, stage?.right) && near(card?.bottom, stage?.bottom)
    && !!scroll && scroll.scrollWidth === scroll.clientWidth;
  check(ctx, label, passed, geometry);
}

function questionCardState(ctx) {
  return ctx.session.eval(`(() => {
    const cards=[...document.querySelectorAll('.next-lanes-scroll .next-question-card')];
    const card=cards[0];
    return {
      count:cards.length,id:card?.dataset.questionId || null,
      label:card?.querySelector('.next-question-card-label')?.textContent.trim() || null,
      tinted:!!card?.classList.contains('is-attention'),
      options:card?.querySelectorAll('.next-question-option').length || 0,
      arrows:card?.querySelectorAll('.next-question-page').length || 0,
    };
  })()`);
}

async function captureScene(ctx, questionId, lane, level, goalId = null, member = null, acceptedQuestionIds = [questionId]) {
  if (level === 'lane') {
    await wait(ctx, `document.querySelector('.next-lanes-scroll')?.dataset.level === 'lane'
      && document.querySelector('.next-lanes-lane-title')?.textContent.trim() === ${JSON.stringify(lane.inventory.title)}
      && ${readExpression(lane.inventory.lane_id, lane.detailRevision)}`, 'lane title and full members read');
  } else if (level === 'goal') {
    await wait(ctx, `document.querySelector('.next-lanes-scroll')?.dataset.level === ${JSON.stringify(level)}
      && document.querySelector('.next-lanes-goal-title')?.textContent.trim() === ${JSON.stringify(lane.goals[0].title)}
      && ${readExpression(lane.inventory.lane_id, lane.detailRevision, goalId)}
      && document.querySelector('.next-lanes-member-title[data-spec-id=${JSON.stringify(member.spec_id)}]')`,
      `${level} goal members and exact spec are ready`);
  } else {
    await wait(ctx, `document.querySelector('.next-lanes-scroll')?.dataset.level === 'spec'
      && document.querySelector('.next-lanes-spec-title')?.textContent.trim() === ${JSON.stringify(member.title)}
      && ${readExpression(lane.inventory.lane_id, lane.detailRevision, goalId)}`,
      'spec title and its resolved goal read are ready');
  }
  const expectedCount = level === 'spec' ? 1 : 2;
  await waitForQuestionCard(ctx, acceptedQuestionIds, expectedCount);
  await selectQuestionCard(ctx, questionId, level === 'spec' ? 1 : 2);
  const card = await questionCardState(ctx);
  const geometry = await phoneGeometry(ctx);
  check(ctx, `E7 ${level} route has the exact placed tinted question card and resolved read`,
    card.id === questionId && card.count === 1 && card.tinted && card.options === 5
      && (level === 'spec' ? card.arrows === 0 && card.label === 'Needs your answer'
        : card.arrows === 2 && /^Question [12] of 2$/.test(card.label || '')),
    { card, questionId, level, goalId, specId: member?.spec_id });
  assertFullPhoneStage(ctx, `E7 ${level} view fills the available phone stage`, geometry);
  return { card, geometry };
}

async function phoneScenario(ctx, ownedIds, hooks) {
  const laneId = F.ORDINARY_A_ID;
  const specA = 'spec_gate__next_lanes_ordinary_a_00';
  const specB = 'spec_gate__next_lanes_ordinary_a_01';
  const options = Array.from({ length: 5 }, (_, index) => ({
    label: `Fixture answer ${String(index + 1).padStart(2, '0')}`,
    value: `fixture-answer-${index + 1}`,
    description: 'Synthetic fixture details wrap naturally within the phone question list.',
  }));
  const q1 = await H.seedQuestion(ctx, ownedIds, {
    key: 'e7-phone-a', specId: specA, title: 'Synthetic phone question A', options,
  });
  const q2 = await H.seedQuestion(ctx, ownedIds, {
    key: 'e7-phone-b', specId: specB, title: 'Synthetic phone question B', options,
  });
  const lane = F.makeOrdinaryLane({
    laneId, title: 'Fixture phone lane', goalTitle: 'Fixture phone goal', memberCount: 3,
    questionSpecIds: [specA, specB],
    questionIdsBySpec: { [specA]: [q1.question_id], [specB]: [q2.question_id] },
  });
  const goalId = lane.goals[0].goal_id;
  const memberA = lane.members.find(member => member.spec_id === specA);
  const frame = F.inventoryFrame([lane]);

  hooks.push(await H.enterNext(ctx, PHONE_WIDTH));
  await H.installLaneBridge(ctx, { frame, versions: H.laneDetailVersions([lane]) });
  await wait(ctx, `document.querySelector('[aria-label="Questions (2)"]')
    && document.querySelector('.next-lanes-row-open[data-lane-id=${JSON.stringify(laneId)}]')`,
    'fixture inventory and question count are rendered');
  const initial = await phoneGeometry(ctx);
  check(ctx, 'E7 phone starts with a full-width top rail, hidden lanes card, and no document overflow',
    initial.viewport?.width === PHONE_WIDTH && initial.viewport?.height === PHONE_HEIGHT
      && near(initial.rail.rect?.left, 0) && near(initial.rail.rect?.top, 0)
      && near(initial.rail.rect?.width, PHONE_WIDTH) && near(initial.rail.rect?.height, 52)
      && initial.card.hidden === true && initial.document.scrollWidth === PHONE_WIDTH
      && initial.document.bodyScrollWidth === PHONE_WIDTH,
    initial);

  await ctx.session.click('[aria-label="Lanes"]');
  await wait(ctx, `!document.querySelector('.lanes-card').hidden
    && !!document.querySelector('.next-lanes-row-open[data-lane-id=${JSON.stringify(laneId)}]')`, 'phone lanes card opens');
  await ctx.session.click(rowSelector(laneId));
  await captureScene(ctx, q1.question_id, lane, 'lane', null, null, [q1.question_id, q2.question_id]);

  const laneCard = await questionCardState(ctx);
  check(ctx, 'E7 two lane questions use one card with a two-page pager',
    laneCard.count === 1 && /^Question [12] of 2$/.test(laneCard.label || '') && laneCard.arrows === 2,
    laneCard);
  await ctx.session.click('.next-lanes-scroll .next-question-next');
  await wait(ctx, `document.querySelector('.next-lanes-scroll .next-question-card')?.dataset.questionId === ${JSON.stringify(q2.question_id)}`,
    'phone pager shows the second lane question');
  const secondCard = await questionCardState(ctx);
  check(ctx, 'E7 the shared lane card selects the other exact question',
    secondCard.id === q2.question_id && /^Question [12] of 2$/.test(secondCard.label || '')
      && secondCard.label !== laneCard.label && secondCard.arrows === 2,
    secondCard);
  await ctx.session.click('.next-lanes-scroll .next-question-next');
  await wait(ctx, `document.querySelector('.next-lanes-scroll .next-question-card')?.dataset.questionId === ${JSON.stringify(q1.question_id)}`,
    'phone question pager wraps to the first exact question');

  await ctx.session.click(`.next-lanes-stop-open[data-goal-id=${JSON.stringify(goalId)}]`);
  await wait(ctx, `document.querySelector('.next-lanes-scroll')?.dataset.level === 'goal'
    && document.querySelector('.next-lanes-goal-title')?.textContent.trim() === ${JSON.stringify(lane.goals[0].title)}
    && ${readExpression(laneId, REVISION, goalId)}
    && document.querySelector('.next-lanes-member-title[data-spec-id=${JSON.stringify(specA)}]')
    && document.querySelector('.next-lanes-member-title[data-spec-id=${JSON.stringify(specB)}]')`,
    'phone goal route has both exact members and its resolved goal read');
  await captureScene(ctx, q1.question_id, lane, 'goal', goalId, memberA, [q1.question_id, q2.question_id]);

  await ctx.session.click(`.next-lanes-member-title[data-spec-id=${JSON.stringify(specA)}]`);
  await wait(ctx, `document.querySelector('.next-lanes-scroll')?.dataset.level === 'spec'
    && document.querySelector('.next-lanes-spec-title')?.textContent.trim() === ${JSON.stringify(memberA.title)}
    && document.querySelector('.next-lanes-scroll .next-question-card')?.dataset.questionId === ${JSON.stringify(q1.question_id)}
    && ${readExpression(laneId, REVISION, goalId)}`,
    'phone spec route has the exact member and question card after its resolved goal read');
  await captureScene(ctx, q1.question_id, lane, 'spec', goalId, memberA);

  await ctx.session.click('.next-lanes-back');
  await wait(ctx, `document.querySelector('.next-lanes-scroll')?.dataset.level === 'goal'
    && document.querySelector('.next-lanes-member-title[data-spec-id=${JSON.stringify(specA)}]')
    && ${readExpression(laneId, REVISION, goalId)}`, 'phone Back returns to the loaded goal');
  await captureScene(ctx, q1.question_id, lane, 'goal', goalId, memberA, [q1.question_id, q2.question_id]);
  await ctx.session.click('.next-lanes-back');
  await wait(ctx, `document.querySelector('.next-lanes-scroll')?.dataset.level === 'lane'
    && document.querySelector('.next-lanes-lane-title')?.textContent.trim() === ${JSON.stringify(lane.inventory.title)}
    && ${readExpression(laneId, REVISION)}`, 'phone Back returns to the loaded lane');
  await captureScene(ctx, q1.question_id, lane, 'lane', null, null, [q1.question_id, q2.question_id]);

  await H.openQuestionList(ctx);
  await wait(ctx, `document.querySelectorAll('.next-question-list-item').length === 2
    && document.querySelector('[aria-label="Questions (2)"]')?.dataset.count === '2'
    && document.querySelector('.questions-popover.next-questions-list:not([hidden])')`,
    'phone question list and rail count are ready');
  const listEvidence = await ctx.session.eval(`(() => {
    const popover=document.querySelector('.questions-popover:not([hidden])');
    const list=popover?.classList.contains('next-questions-list') ? popover : null;
    const items=[...(list?.querySelectorAll('.next-question-list-item') || [])];
    const r=popover?.getBoundingClientRect();
    const lr=list?.getBoundingClientRect();
    const before=popover?.scrollTop || 0;
    if (popover) popover.scrollTop=popover.scrollHeight;
    const scrolled=popover?.scrollTop || 0;
    if (popover) popover.scrollTop=before;
    return {
      railCount:document.querySelector('[aria-label="Questions (2)"]')?.dataset.count || null,
      ids:items.map(item=>item.dataset.questionId),
      forms:items.map(item=>!!item.querySelector('.next-question-form')),
      viewport:{width:innerWidth,height:innerHeight},
      document:{clientWidth:document.documentElement.clientWidth,scrollWidth:document.documentElement.scrollWidth,
        bodyClientWidth:document.body.clientWidth,bodyScrollWidth:document.body.scrollWidth,scrollX},
      popover:{left:r?.left,top:r?.top,right:r?.right,bottom:r?.bottom,width:popover?.clientWidth,
        clientHeight:popover?.clientHeight,scrollHeight:popover?.scrollHeight,scrollWidth:popover?.scrollWidth,
        overflowX:getComputedStyle(popover).overflowX,overflowY:getComputedStyle(popover).overflowY,scrolled},
      list:{left:lr?.left,right:lr?.right,width:list?.clientWidth,scrollWidth:list?.scrollWidth},
      listSameNode:list === popover,
    };
  })()`);
  const ids = listEvidence.ids || [];
  const popover = listEvidence.popover;
  const list = listEvidence.list;
  const listFitsAndScrollsVertically = listEvidence.railCount === '2'
    && listEvidence.listSameNode === true
    && ids.length === 2 && ids.includes(q1.question_id) && ids.includes(q2.question_id)
    && listEvidence.forms.every(Boolean)
    && listEvidence.viewport?.width === PHONE_WIDTH && listEvidence.viewport?.height === PHONE_HEIGHT
    && listEvidence.document?.clientWidth === PHONE_WIDTH && listEvidence.document?.scrollWidth === PHONE_WIDTH
    && listEvidence.document?.bodyClientWidth === PHONE_WIDTH && listEvidence.document?.bodyScrollWidth === PHONE_WIDTH
    && listEvidence.document?.scrollX === 0
    && popover?.left >= 0 && popover?.top >= 52 && popover?.right <= PHONE_WIDTH && popover?.bottom <= PHONE_HEIGHT
    && popover?.scrollWidth === popover?.width && popover?.scrollHeight > popover?.clientHeight
    && ['auto', 'scroll'].includes(popover?.overflowY) && popover?.scrolled > 0
    && list?.left >= popover?.left && list?.right <= popover?.right && list?.scrollWidth === list?.width;
  check(ctx, 'E7 question list fits its popover and scrolls vertically without document or list horizontal overflow',
    listFitsAndScrollsVertically, listEvidence);
  const finalGeometry = await phoneGeometry(ctx);
  assertFullPhoneStage(ctx, 'E7 question-list overlay keeps the full phone stage, rail, and hidden assistant', finalGeometry);
}

async function cleanup(ctx, ownedIds, hooks, primaryError) {
  const errors = [];
  let beforeCleanup = null;
  let releaseAllReceipt = null;
  let afterRelease = null;
  try { beforeCleanup = await ctx.session.eval('window.__nextLanesGetEvidence?.() || null'); }
  catch (error) { errors.push(error); }
  for (const captureError of beforeCleanup?.captureErrors || []) {
    const error = new Error(`before-cleanup evidence capture failed: ${captureError.source || 'unknown source'}`);
    error.cause = captureError.error || captureError;
    errors.push(error);
  }
  try {
    releaseAllReceipt = await ctx.session.eval(`(() => {
      const control=window.__nextLanes;
      return typeof control?.releaseAll === 'function' ? control.releaseAll() : window.__nextLanesReleaseAll?.();
    })()`);
  } catch (error) { errors.push(error); }
  try { afterRelease = await ctx.session.eval('window.__nextLanesGetEvidence?.() || null'); }
  catch (error) { errors.push(error); }
  for (const captureError of afterRelease?.captureErrors || []) {
    const error = new Error(`after-release evidence capture failed: ${captureError.source || 'unknown source'}`);
    error.cause = captureError.error || captureError;
    errors.push(error);
  }
  if ((beforeCleanup?.pending || []).length) {
    const receipts = releaseAllReceipt?.calls || [];
    const releaseComplete = releaseAllReceipt?.released === true
      && releaseAllReceipt.frameProcessed === true
      && releaseAllReceipt.count === beforeCleanup.pending.length
      && receipts.length === beforeCleanup.pending.length
      && receipts.every(receipt => receipt.status === 'resolved'
        && receipt.frameProcessed === true
        && receipt.requestedRevision === receipt.answeredRevision);
    if (!releaseComplete || (afterRelease?.pending || []).length) {
      errors.push(new Error(`release-all did not resolve and flush every held phone read: ${JSON.stringify({
        pendingBefore: beforeCleanup.pending,
        releaseAllReceipt,
        pendingAfter: afterRelease?.pending || null,
      })}`));
    }
  }
  try { await H.cleanupQuestions(ctx, ownedIds, ctx.report); }
  catch (error) { errors.push(error); }
  for (const hook of hooks.reverse()) {
    try {
      if (!hook?.identifier) throw new Error('phone scenario document hook has no removal identifier');
      await H.cleanupHook(ctx, hook);
    } catch (error) { errors.push(error); }
  }
  try { await ctx.session.send('Emulation.clearDeviceMetricsOverride'); }
  catch (error) { errors.push(error); }
  try {
    await ctx.session.send('Page.navigate', { url: ctx.url });
    await ctx.session.waitFor("document.readyState === 'complete' && !!document.getElementById('session-list')", { timeoutMs: ctx.timeoutMs });
  } catch (error) { errors.push(error); }
  try {
    ctx.report.diagnostic('next-lanes phone scenario cleanup', {
      scenarioError: primaryError ? errorDetail(primaryError) : null,
      beforeCleanup: beforeCleanup ? {
        frameSequence: beforeCleanup.frameSequence,
        pending: beforeCleanup.pending || [],
        calls: beforeCleanup.calls || [],
        releaseReceipts: beforeCleanup.releaseReceipts || [],
        sourceEvents: beforeCleanup.sourceEvents || [],
      } : null,
      releaseAllReceipt,
      afterRelease: afterRelease ? {
        frameSequence: afterRelease.frameSequence,
        pending: afterRelease.pending || [],
        calls: afterRelease.calls || [],
        releaseReceipts: afterRelease.releaseReceipts || [],
        sourceEvents: afterRelease.sourceEvents || [],
      } : null,
      ownedQuestionIds: [...ownedIds], cleanupErrors: errors.map(errorDetail),
    });
  } catch (error) { errors.push(error); }
  try {
    ctx.report.ok('phone scenario cleanup completed', errors.length === 0,
      { errors: errors.map(errorDetail) });
  } catch (error) { errors.push(error); }
  if (primaryError) {
    if (errors.length && typeof primaryError === 'object') primaryError.cleanupErrors = errors.map(errorDetail);
    throw primaryError;
  }
  if (errors.length) throw new AggregateError(errors, 'phone scenario cleanup failed');
}

async function nextLanesPhone(ctx) {
  const ownedIds = [];
  const hooks = [];
  let primaryError = null;
  try { await phoneScenario(ctx, ownedIds, hooks); }
  catch (error) {
    primaryError = error;
    try { ctx.report.ok('phone scenario execution failed', false, errorDetail(error)); }
    catch (_) { /* Preserve the scenario error and continue cleanup. */ }
  }
  await cleanup(ctx, ownedIds, hooks, primaryError);
}

module.exports = { nextLanesPhone };
