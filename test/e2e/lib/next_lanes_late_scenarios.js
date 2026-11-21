'use strict';

const {
  PRODUCER,
  cleanupQuestions,
  enterNext,
  installLaneBridge,
  laneDetailVersions,
  notificationFor,
  openQuestionList,
  QUESTION_PREFIX,
  reconnectAndReplaceSubscription,
  seedQuestion,
  waitForQuestion,
} = require('./next_lanes_harness');
const { fixtureRequest } = require('./closed_chat_scenario');
const { revisionRequestsCoalesce } = require('./next_lanes_revision_contract');
const {
  L11_ID,
  L65_ID,
  ORDINARY_A_ID,
  ORDINARY_B_ID,
  PARKED_ID,
  REVISION_A,
  REVISION_B,
  inventoryFrame,
  makeL11,
  makeL65,
  makeMember,
  makeOrdinaryLane,
  makeParkedLane,
  fixtureLaneId,
  staleQuestionPreview,
  e9MovedRevisionFixture,
} = require('./next_lanes_fixtures');

function questionIdForKey(key) {
  return `${QUESTION_PREFIX}${String(key).replace(/[^a-zA-Z0-9_-]/g, '_')}`;
}

function questionSelector(questionId) {
  return `.next-question-list-item[data-question-id=${JSON.stringify(questionId)}]`;
}

function check(report, label, condition, evidence) {
  const passed = Boolean(condition);
  report.ok(label, passed, evidence);
  if (!passed) throw new Error(`${label} failed`);
}

async function questionEvidence(session, questionId) {
  const selector = questionSelector(questionId);
  return session.eval(`(() => {
    const row = document.querySelector(${JSON.stringify(selector)});
    if (!row) return null;
    const link = row.querySelector('.next-question-heading-link');
    return {
      visible: !row.closest('[hidden]'),
      ownTitle: row.querySelector('.next-question-heading-title')?.textContent || null,
      linkTitle: link?.textContent.trim() || null,
      laneTitle: link?.querySelector('.next-question-lane-title')?.textContent || null,
      goalTitle: link?.querySelector('.next-question-goal-title')?.textContent || null,
      isChatLink: !!row.querySelector('.next-question-chat-link'),
      retry: !!row.querySelector('.next-question-retry'),
      hasForm: !!row.querySelector('.next-question-form'),
      optionCount: row.querySelectorAll('.next-question-option').length,
      hasSend: !!row.querySelector('.next-question-send'),
      sendDisabled: row.querySelector('.next-question-send')?.disabled ?? true,
    };
  })()`);
}

async function visibleGoalMemberRows(session) {
  return session.eval(`([...document.querySelectorAll('.next-lanes-scroll .next-lanes-member-title[data-spec-id]')]
    .map(row => ({ specId: row.dataset.specId, goalId: row.dataset.goalId || null })))`);
}

async function revealGoalMemberRows(session) {
  for (let attempt = 0; attempt < 32; attempt += 1) {
    const changed = await session.eval(`(() => {
      const root=document.querySelector('.next-lanes-scroll');
      const collapsedDone=[...(root?.querySelectorAll('.next-lanes-section-done')||[])]
        .find(section => !section.querySelector('.next-lanes-member-list'));
      const doneToggle=collapsedDone?.querySelector('.next-lanes-section-toggle');
      if (doneToggle) { doneToggle.click(); return true; }
      const more=root?.querySelector('.next-lanes-show-more');
      if (more) { more.click(); return true; }
      return false;
    })()`);
    if (!changed) return;
  }
  throw new Error('goal member sections did not finish expanding');
}

async function laneCallEvidence(session, laneId) {
  return session.eval(`window.__nextLanes.calls
    .filter(call => call.laneId === ${JSON.stringify(laneId)})
    .map(call => ({ laneId: call.laneId, revision: call.revision, membersLimit: Number(call.options.membersLimit),
      goalId: call.options.goalId || null, membersAfter: call.options.membersAfter || null }))`);
}

function blockedReason(goal) {
  const blocked = Math.min(Number(goal?.blocked || 0), Math.max(0, Number(goal?.total || 0) - Number(goal?.done || 0)));
  return blocked ? `${blocked} blocked` : 'blocked';
}

function flushReceipt(receipt) {
  if (!receipt) return receipt;
  const { renderFrame, ...fields } = receipt;
  return { ...fields, ...(renderFrame === undefined ? {} : { flushSequence: renderFrame }) };
}

async function resolvedGoalReadEvidence(session, laneId, revision, goalId) {
  return session.eval(`(() => {
    const call = [...window.__nextLanes.calls].reverse().find(item => item.laneId === ${JSON.stringify(laneId)}
      && item.revision === ${JSON.stringify(revision)} && item.options.goalId === ${JSON.stringify(goalId)}
      && item.status === 'resolved' && item.answeredRevision === ${JSON.stringify(revision)});
    if (!call?.reply) return null;
    return {
      callId: call.callId,
      status: call.status,
      requestedGoalId: call.options.goalId ?? null,
      requestedRevision: call.revision,
      answeredRevision: call.answeredRevision,
      replyRevision: call.reply.detail_revision,
      members: (call.reply.members || []).map(member => ({ specId: member.spec_id, goalId: member.goal_id ?? null })),
    };
  })()`);
}

async function startQuestionHeadingObserver(session, questionIds) {
  return session.eval(`(() => {
    const ids = new Set(${JSON.stringify(questionIds)});
    const history = [];
    const previous = new Map();
    const capture = phase => {
      const seen = new Set();
      for (const row of document.querySelectorAll('.next-question-list-item, .next-question-card')) {
        const questionId = row.dataset.questionId;
        if (!ids.has(questionId)) continue;
        const surface = row.classList.contains('next-question-list-item') ? 'list' : 'card';
        const key = questionId + ':' + surface;
        const heading = row.querySelector('.next-question-list-heading') || row.querySelector('.next-question-about');
        const state = {
          questionId,
          surface,
          present: true,
          visible: !row.closest('[hidden]'),
          headingText: heading?.textContent?.trim() || null,
          ownTitle: row.querySelector('.next-question-heading-title')?.textContent?.trim() || null,
          linkText: row.querySelector('.next-question-heading-link')?.textContent?.trim() || null,
          chatLink: !!row.querySelector('.next-question-chat-link'),
          retry: !!row.querySelector('.next-question-retry'),
        };
        const serialized = JSON.stringify(state);
        if (previous.get(key) !== serialized) {
          history.push({ sequence: history.length + 1, phase, ...state });
          previous.set(key, serialized);
        }
        seen.add(key);
      }
      for (const [key, serialized] of previous) {
        if (!seen.has(key)) {
          const prior = JSON.parse(serialized);
          if (prior.present) {
            history.push({ sequence: history.length + 1, phase, questionId: prior.questionId,
              surface: prior.surface, present: false, headingText: null, ownTitle: null, linkText: null, chatLink: false });
            previous.set(key, JSON.stringify({ ...prior, present: false }));
          }
        }
      }
    };
    const observer = new MutationObserver(() => capture('mutation'));
    observer.observe(document.documentElement, { subtree: true, childList: true, characterData: true, attributes: true });
    capture('initial');
    window.__nextLanesQuestionHeadingGuard = {
      snapshot() {
        capture('snapshot');
        return history.slice();
      },
      stop() {
        observer.disconnect();
        return this.snapshot();
      },
    };
    return { installed: true, observedIds: [...ids] };
  })()`);
}

function truncatedInventoryFrame(includedLanes, omittedLane) {
  const includedActive = includedLanes.filter(lane => lane.inventory.state !== 'paused').length;
  const fillerCount = 63 - includedActive;
  if (fillerCount < 0 || omittedLane.inventory.state !== 'paused') {
    throw new Error('truncated fixture must contain 63 active rows and two paused rows');
  }
  const fillers = Array.from({ length: fillerCount }, (_, index) => {
    const laneId = fixtureLaneId(`truncated-pad-${String(index).padStart(2, '0')}`);
    const title = `Fixture truncation pad ${index + 1}`;
    const base = makeOrdinaryLane({ laneId, title, memberCount: 1 });
    const goalId = base.goals[0].goal_id;
    const member = makeMember(`spec_gate__next_lanes_truncated_pad_${String(index).padStart(2, '0')}`, goalId);
    return makeOrdinaryLane({ laneId, title, goalId, members: [member] });
  });
  return inventoryFrame([...includedLanes, ...fillers, omittedLane], { open: 65, truncated: true });
}

async function laneDetailStops(session) {
  return session.eval(`(() => {
    const body = document.querySelector('.next-lanes-scroll');
    return {
      level: body?.dataset.level || null,
      stops: [...(body?.querySelectorAll('.next-lanes-stop-open') || [])].map(button => ({
        goalId: button.dataset.goalId || null,
        title: button.querySelector('.next-lanes-stop-title')?.textContent || '',
        number: button.querySelector('.next-lanes-stop-number')?.textContent || null,
        defaultGoal: button.dataset.defaultGoal === 'true',
      })),
      more: body?.querySelector('.next-lanes-more-stops')?.textContent || null,
    };
  })()`);
}

async function currentFrameEvidence(session) {
  return session.eval(`(() => {
    const evidence = window.__nextLanesGetEvidence();
    const frame = evidence.rawFrame;
    return {
      frameSequence: evidence.frameSequence,
      flushSequence: evidence.renderFrame,
      truncated: !!frame?.truncated,
      laneCount: frame?.lanes?.length || 0,
      counts: frame?.counts || null,
      laneIds: (frame?.lanes || []).map(lane => lane.lane_id),
      states: (frame?.lanes || []).map(lane => lane.state),
      revisions: Object.fromEntries((frame?.lanes || []).map(lane => [lane.lane_id, lane.detail_revision])),
      normalizedInventory: evidence.normalizedInventory,
    };
  })()`);
}

async function laneBridgeEvidence(session) {
  return session.eval('window.__nextLanesGetEvidence()');
}

function normalizedLane(evidence, laneId) {
  return evidence?.normalizedInventory?.lanes?.find(lane => lane.lane_id === laneId) || null;
}

function rawLane(frame, laneId) {
  return frame?.lanes?.find(lane => lane.lane_id === laneId) || null;
}

async function laneRootEvidence(session, laneId) {
  return session.eval(`(() => {
    const open = document.querySelector('.next-lanes-row-open[data-lane-id=${JSON.stringify(laneId)}]');
    const row = open?.closest('.next-lanes-row');
    if (!row) return null;
    return {
      title: row.querySelector('.next-lanes-row-title')?.textContent || null,
      retryKinds: [...row.querySelectorAll('.next-lanes-retry')].map(button => button.dataset.readKind),
      attention: [...row.querySelectorAll('.next-lanes-attention')].map(line => ({
        title: line.querySelector('.next-lanes-attention-goal')?.textContent || '',
        reason: line.querySelector('.next-lanes-attention-reason')?.textContent || '',
      })),
      more: row.querySelector('.next-lanes-more')?.textContent || null,
      questionMark: !!row.querySelector('.next-lanes-question-mark'),
    };
  })()`);
}

async function waitForQuestionRows(ctx, questions) {
  await openQuestionList(ctx);
  for (const question of questions) {
    await waitForQuestion(ctx, question.question_id);
    const notification = await notificationFor(ctx, question.question_id);
    if (notification?.question?.question_id !== question.question_id) {
      throw new Error(`synthetic question notification did not reach the list: ${question.question_id}`);
    }
  }
}

async function emitInventory(session, frame) {
  return session.eval(`(async () => {
    const value = ${JSON.stringify(frame)};
    if (typeof window.__nextLanes?.setInventory === 'function') return await window.__nextLanes.setInventory(value);
    return await window.__nextLanesSetInventory(value);
  })()`);
}

async function selectQuestionCard(session, questionId, timeoutMs = 10000) {
  for (let index = 0; index < 12; index += 1) {
    const current = await session.eval("document.querySelector('.next-lanes-scroll .next-question-card')?.dataset.questionId || null");
    if (current === questionId) return current;
    if (!current) break;
    const clicked = await session.eval(`(() => {
      const next = document.querySelector('.next-lanes-scroll .next-question-next');
      if (!next) return false;
      next.click();
      return true;
    })()`);
    if (!clicked) break;
    await session.waitFor(`document.querySelector('.next-lanes-scroll .next-question-card')?.dataset.questionId !== ${JSON.stringify(current)}`, { timeoutMs });
  }
  const selected = await session.eval("document.querySelector('.next-lanes-scroll .next-question-card')?.dataset.questionId || null");
  if (selected !== questionId) throw new Error(`question card pager did not select target ${questionId} (selected ${selected})`);
  return selected;
}

async function hasOpenQuestionsControl(session, count) {
  return session.eval(`document.querySelector('[aria-label="Questions (${count})"]')?.dataset.count === ${JSON.stringify(String(count))}`);
}

async function closeQuestionList(session) {
  const open = await session.eval("document.querySelector('[aria-label^=Questions]')?.classList.contains('open') || false");
  if (open) await session.click('[aria-label^="Questions"]');
}

function errorDetail(error) {
  const detail = {
    name: error?.name || 'Error',
    message: String(error?.message || error),
  };
  if (error?.stack) detail.stack = String(error.stack);
  if (error?.cleanupError) detail.cleanupError = typeof error.cleanupError === 'object'
    ? { ...error.cleanupError }
    : String(error.cleanupError);
  if (Array.isArray(error?.cleanupErrors)) detail.cleanupErrors = error.cleanupErrors.map(errorDetail);
  if (error?.cause) detail.cause = typeof error.cause === 'object' ? errorDetail(error.cause) : String(error.cause);
  if (Array.isArray(error?.errors)) detail.errors = error.errors.map(errorDetail);
  return detail;
}

async function ownedQuestionStates(ctx, ownedIds) {
  const states = [];
  for (const questionId of ownedIds) {
    try {
      const reply = await fixtureRequest(ctx.runtime, PRODUCER, { type: 'prompt.status', question_id: questionId });
      states.push({ questionId, ok: reply?.ok === true, state: reply?.question?.state || null, errorCode: reply?.error_code || null });
    } catch (error) {
      states.push({ questionId, ok: false, state: null, error: errorDetail(error) });
    }
  }
  return states;
}

async function cleanupEvidence(ctx, ownedIds) {
  let evidence = null;
  const captureErrors = [];
  try { evidence = await laneBridgeEvidence(ctx.session); }
  catch (error) { captureErrors.push({ source: 'lane-bridge-evidence', error: errorDetail(error) }); }
  let readState = null;
  try {
    readState = await ctx.session.eval(`(() => {
      const control = window.__nextLanes;
      if (!control) return null;
      const shape = call => ({
        laneId: call.laneId,
        revision: call.revision || null,
        membersLimit: Number(call.options?.membersLimit) || null,
        goalId: call.options?.goalId || null,
        membersAfter: call.options?.membersAfter || null,
      });
      return {
        calls: (control.calls || []).map(shape),
        pending: (control.pending || []).map(shape),
        releaseReceipts: (control.releaseReceipts || []).map(receipt => ({ ...receipt })),
      };
    })()`);
  } catch (error) { captureErrors.push({ source: 'lane-read-state', error: errorDetail(error) }); }
  let headingHistory = null;
  try { headingHistory = await ctx.session.eval('window.__nextLanesQuestionHeadingGuard?.snapshot() ?? null'); }
  catch (error) { captureErrors.push({ source: 'question-heading-history', error: errorDetail(error) }); }
  const frame = evidence?.rawFrame;
  return {
    frameSequence: evidence?.frameSequence ?? null,
    flushSequence: evidence?.renderFrame ?? null,
    headingHistory,
    sourceOwned: evidence?.sourceOwned ?? null,
    sourceEvents: evidence?.sourceEvents || [],
    frames: (evidence?.acceptedFrames || []).map(item => ({
      frameSequence: item.frameSequence,
      flushSequence: item.renderFrame,
      laneIds: item.rawFrame?.lanes?.map(lane => lane.lane_id) || [],
      revisions: Object.fromEntries((item.rawFrame?.lanes || []).map(lane => [lane.lane_id, lane.detail_revision])),
    })),
    currentLaneIds: frame?.lanes?.map(lane => lane.lane_id) || [],
    currentRevisions: Object.fromEntries((frame?.lanes || []).map(lane => [lane.lane_id, lane.detail_revision])),
    calls: evidence?.calls || readState?.calls || [],
    pending: evidence?.pending || readState?.pending || [],
    releaseReceipts: evidence?.releaseReceipts || readState?.releaseReceipts || [],
    captureErrors,
    ownedQuestions: await ownedQuestionStates(ctx, ownedIds),
  };
}

function isolated(run) {
  return async ctx => {
    const ownedIds = [];
    const hooks = [];
    let scenarioError = null;
    try {
      await run(ctx, { ownedIds, hooks });
    } catch (error) {
      scenarioError = error;
    }

    const cleanupErrors = [];
    let beforeCleanup = null;
    let releaseAllReceipt = null;
    let afterRelease = null;
    let afterQuestions = null;
    try { beforeCleanup = await cleanupEvidence(ctx, ownedIds); }
    catch (error) { cleanupErrors.push(error); }
    for (const capture of beforeCleanup?.captureErrors || []) {
      const error = new Error(`before-cleanup evidence capture failed: ${capture.source}`);
      error.cause = capture.error;
      cleanupErrors.push(error);
    }

    // Snapshot evidence above is read-only. Release every held page before any
    // question, hook, metrics, or navigation cleanup mutates the fixture.
    try {
      releaseAllReceipt = await ctx.session.eval(`(() => {
        const control = window.__nextLanes;
        return typeof control?.releaseAll === 'function' ? control.releaseAll() : window.__nextLanesReleaseAll?.();
      })()`);
    } catch (error) { cleanupErrors.push(error); }
    try { afterRelease = await cleanupEvidence(ctx, ownedIds); }
    catch (error) { cleanupErrors.push(error); }
    for (const capture of afterRelease?.captureErrors || []) {
      const error = new Error(`after-release evidence capture failed: ${capture.source}`);
      error.cause = capture.error;
      cleanupErrors.push(error);
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
        cleanupErrors.push(new Error(`release-all did not resolve and flush every held read: ${JSON.stringify({
          pendingBefore: beforeCleanup.pending,
          releaseAllReceipt,
          pendingAfter: afterRelease?.pending || null,
        })}`));
      }
    }

    try { await cleanupQuestions(ctx, ownedIds, ctx.report); }
    catch (error) { cleanupErrors.push(error); }
    try { afterQuestions = await ownedQuestionStates(ctx, ownedIds); }
    catch (error) { cleanupErrors.push(error); }

    for (const hook of hooks) {
      if (!hook) {
        cleanupErrors.push(new Error('new-document hook reference is missing before removal'));
        continue;
      }
      try {
        if (!hook.identifier) throw new Error('new-document hook has no removal identifier');
        await ctx.session.send('Page.removeScriptToEvaluateOnNewDocument', { identifier: hook.identifier });
      } catch (error) { cleanupErrors.push(error); }
    }
    try { await ctx.session.send('Emulation.clearDeviceMetricsOverride'); }
    catch (error) { cleanupErrors.push(error); }
    try { await ctx.session.send('Page.navigate', { url: ctx.url }); }
    catch (error) { cleanupErrors.push(error); }
    try {
      await ctx.session.waitFor("document.readyState === 'complete' && !!document.getElementById('session-list')", { timeoutMs: ctx.timeoutMs });
    } catch (error) { cleanupErrors.push(error); }

    try {
      ctx.report.diagnostic('next-lanes late scenario and cleanup', {
        scenarioError: scenarioError ? errorDetail(scenarioError) : null,
        beforeCleanup,
        releaseAllReceipt,
        afterRelease,
        afterQuestions,
        cleanupErrors: cleanupErrors.map(errorDetail),
      });
    } catch (error) { cleanupErrors.push(error); }

    if (scenarioError) {
      if (cleanupErrors.length && scenarioError && typeof scenarioError === 'object') {
        try { scenarioError.cleanupErrors = cleanupErrors.map(errorDetail); } catch (_) { /* preserve primary error */ }
      }
      throw scenarioError;
    }
    if (cleanupErrors.length) {
      throw new AggregateError(cleanupErrors, 'next-lanes late scenario cleanup failed');
    }
  };
}

async function runDataRecovery(ctx, { ownedIds, hooks }) {
  const { session, report } = ctx;
  const previewId = questionIdForKey('e8-preview');
  const offPreviewId = questionIdForKey('e8-off-preview');
  const noLaneId = questionIdForKey('e8-no-lane');
  const ordinaryId = questionIdForKey('e8-ordinary');
  const noRoadQuestionId = questionIdForKey('e8-l65-preview');
  const l11 = makeL11({
    previewQuestionIds: [previewId],
    offPreviewQuestionIds: [offPreviewId],
  });
  const ordinaryBase = makeOrdinaryLane({ laneId: ORDINARY_A_ID, title: 'Fixture unaffected lane' });
  const ordinaryMembers = ordinaryBase.members.map((member, index) => index === 0
    ? { ...member, open_questions: { count: 1, first_question_id: ordinaryId } }
    : member);
  const ordinary = makeOrdinaryLane({
    laneId: ORDINARY_A_ID,
    title: 'Fixture unaffected lane',
    goalId: ordinaryBase.goals[0].goal_id,
    goalTitle: ordinaryBase.goals[0].title,
    members: ordinaryMembers,
  });
  const noRoad = makeL65({ questionId: noRoadQuestionId });
  const previewMember = l11.members.find(member => member.spec_id === l11.questionSpecIds.preview);
  const offPreviewMember = l11.members.find(member => member.spec_id === l11.questionSpecIds.offPreview);
  const previewGoal = l11.goals.find(goal => goal.goal_id === previewMember.goal_id);
  const offPreviewGoal = l11.goals.find(goal => goal.goal_id === offPreviewMember.goal_id);

  hooks.push(await enterNext(ctx));
  const frame = inventoryFrame([l11, ordinary, noRoad]);
  await installLaneBridge(ctx, {
    frame,
    versions: laneDetailVersions([l11, ordinary, noRoad]),
    failures: [L11_ID],
    holdRules: [
      { laneId: L65_ID, goalId: null, membersLimit: 1, remaining: 1 },
      { laneId: L65_ID, goalId: null, membersLimit: 32, remaining: 1 },
    ],
  });

  await session.waitFor(`window.__nextLanes.calls.some(call => call.laneId === ${JSON.stringify(L11_ID)}
    && call.revision === ${JSON.stringify(REVISION_A)} && Number(call.options.membersLimit) === 1)`, { timeoutMs: ctx.timeoutMs });
  await session.waitFor(`!!document.querySelector('.next-lanes-row-open[data-lane-id=${JSON.stringify(L11_ID)}]')`, { timeoutMs: ctx.timeoutMs });
  await session.waitFor(`!!document.querySelector('.next-lanes-row-open[data-lane-id=${JSON.stringify(L11_ID)}]')?.closest('.next-lanes-row').querySelector('.next-lanes-retry[data-read-kind="goals"]')`, { timeoutMs: ctx.timeoutMs });

  const rootFailed = await laneRootEvidence(session, L11_ID);
  const failedL11Calls = await laneCallEvidence(session, L11_ID);
  check(report, 'E8: the failed L11 goals read and root Retry are observed before question-driven membership reads',
    rootFailed?.retryKinds.includes('goals') && rootFailed.attention.length === 0 && rootFailed.more === null
      && failedL11Calls.some(call => call.revision === REVISION_A && call.membersLimit === 1)
      && !failedL11Calls.some(call => call.revision === REVISION_A && call.membersLimit === 32),
    { failedL11Calls });

  const previewQuestion = await seedQuestion(ctx, ownedIds, {
    key: 'e8-preview', specId: l11.questionSpecIds.preview, title: 'Synthetic preview question',
  });
  await waitForQuestionRows(ctx, [previewQuestion]);
  const offPreviewQuestion = await seedQuestion(ctx, ownedIds, {
    key: 'e8-off-preview', specId: l11.questionSpecIds.offPreview, title: 'Synthetic off-preview question',
  });
  const noLaneQuestion = await seedQuestion(ctx, ownedIds, {
    key: 'e8-no-lane', specId: 'spec_gate__e8_outside_all_lanes', title: 'Synthetic no-lane question',
  });
  const ordinaryQuestion = await seedQuestion(ctx, ownedIds, {
    key: 'e8-ordinary', specId: ordinary.members[0].spec_id, title: 'Synthetic unaffected question',
  });
  const noRoadQuestion = await seedQuestion(ctx, ownedIds, {
    key: 'e8-l65-preview', specId: noRoad.questionSpecId, title: 'Synthetic no-road preview question',
  });
  const questions = [previewQuestion, offPreviewQuestion, noLaneQuestion, ordinaryQuestion, noRoadQuestion];
  await waitForQuestionRows(ctx, questions.slice(1));
  const ids = questions.map(question => question.question_id);

  const previewFailed = await questionEvidence(session, ids[0]);
  const offPreviewFailed = await questionEvidence(session, ids[1]);
  const noLaneFailed = await questionEvidence(session, ids[2]);
  const ordinaryUnaffected = await questionEvidence(session, ids[3]);
  const noRoadPreview = await questionEvidence(session, ids[4]);
  const failedL11AfterQuestions = await laneCallEvidence(session, L11_ID);
  check(report, 'E8: previewed and off-preview L11 questions keep their own titles, Retry, and answer forms',
    previewFailed?.ownTitle === 'Synthetic preview question' && !previewFailed.linkTitle && previewFailed.retry && !previewFailed.isChatLink
      && previewFailed.hasForm && previewFailed.hasSend && previewFailed.optionCount > 0
      && offPreviewFailed?.ownTitle === 'Synthetic off-preview question' && !offPreviewFailed.linkTitle && offPreviewFailed.retry && !offPreviewFailed.isChatLink
      && offPreviewFailed.hasForm && offPreviewFailed.hasSend && offPreviewFailed.optionCount > 0,
    { previewFailed, offPreviewFailed });
  for (const questionId of ids.slice(0, 2)) {
    const row = questionSelector(questionId);
    const send = `${row} .next-question-send`;
    await session.click(`${row} .next-question-option`);
    await session.waitFor(`document.querySelector(${JSON.stringify(send)})?.disabled === false`, { timeoutMs: ctx.timeoutMs });
  }
  const selectedPreviewFailed = await questionEvidence(session, ids[0]);
  const selectedOffPreviewFailed = await questionEvidence(session, ids[1]);
  check(report, 'E8: both failed-read L11 question forms accept a choice and enable Send',
    selectedPreviewFailed?.hasForm && selectedPreviewFailed.hasSend && selectedPreviewFailed.optionCount > 0 && !selectedPreviewFailed.sendDisabled
      && selectedOffPreviewFailed?.hasForm && selectedOffPreviewFailed.hasSend && selectedOffPreviewFailed.optionCount > 0 && !selectedOffPreviewFailed.sendDisabled,
    { selectedPreviewFailed, selectedOffPreviewFailed });
  check(report, 'E8: a no-lane spec also shows Retry while L11 could not be searched',
    noLaneFailed?.ownTitle === 'Synthetic no-lane question' && !noLaneFailed.linkTitle && noLaneFailed.retry && !noLaneFailed.isChatLink && noLaneFailed.optionCount > 0,
    noLaneFailed);
  check(report, 'E8: the ordinary lane question remains placed with pristine answer options and a disabled Send',
    ordinaryUnaffected?.linkTitle?.includes('Fixture unaffected lane')
      && ordinaryUnaffected.goalTitle === ordinary.goals[0].title && !ordinaryUnaffected.retry
      && ordinaryUnaffected.hasForm && ordinaryUnaffected.hasSend
      && ordinaryUnaffected.optionCount > 0 && ordinaryUnaffected.sendDisabled,
    ordinaryUnaffected);
  check(report, 'E8: the accepted L65 preview question has pristine answer options and a disabled Send before the goals-only read',
    noRoadPreview?.linkTitle?.includes('Fixture L65 lane')
      && noRoadPreview.goalTitle === noRoad.goals.find(goal => goal.goal_id === noRoad.members.find(member => member.spec_id === noRoad.questionSpecId).goal_id)?.title
      && !noRoadPreview.retry && noRoadPreview.hasForm && noRoadPreview.hasSend
      && noRoadPreview.optionCount > 0 && noRoadPreview.sendDisabled,
    noRoadPreview);
  check(report, 'E8: off-preview questions trigger the full L11 membership read only after the root failure was observed',
    failedL11AfterQuestions.some(call => call.revision === REVISION_A && call.membersLimit === 32),
    { failedL11AfterQuestions });
  check(report, 'E8: all five questions remain counted while lane detail is unavailable',
    await hasOpenQuestionsControl(session, 5), { ids });

  await session.waitFor(`window.__nextLanes.pending.some(item => item.laneId === ${JSON.stringify(L65_ID)}
    && item.revision === ${JSON.stringify(noRoad.detailRevision)} && Number(item.options.membersLimit) === 1 && item.options.goalId == null)`, { timeoutMs: ctx.timeoutMs });
  const rootNoRoadBeforeGoals = await laneRootEvidence(session, L65_ID);
  check(report, 'E8: the capped L65 root has no goal lines before its held goals-only read',
    rootNoRoadBeforeGoals?.attention.length === 0 && rootNoRoadBeforeGoals.more === null,
    rootNoRoadBeforeGoals);
  const noRoadGoalsRelease = await session.eval(`window.__nextLanesReleaseMatchingAndFlush({
    laneId: ${JSON.stringify(L65_ID)}, goalId: null, membersLimit: 1, revision: ${JSON.stringify(noRoad.detailRevision)}
  })`);
  check(report, 'E8: the L65 goals-only page is released at the current revision',
    noRoadGoalsRelease?.released === true && noRoadGoalsRelease.status === 'resolved'
      && noRoadGoalsRelease.requestedRevision === noRoad.detailRevision
      && noRoadGoalsRelease.answeredRevision === noRoad.detailRevision && noRoadGoalsRelease.frameProcessed === true,
    flushReceipt(noRoadGoalsRelease));
  const noRoadQuestionGoal = noRoad.goals.find(goal => goal.goal_id
    === noRoad.members.find(member => member.spec_id === noRoad.questionSpecId)?.goal_id);
  const noRoadFirstBlockedGoal = noRoad.goals.find(goal => noRoad.members.some(member => member.goal_id === goal.goal_id && member.status === 'blocked'));
  await session.waitFor(`(() => {
    const row=document.querySelector('.next-lanes-row-open[data-lane-id=${JSON.stringify(L65_ID)}]')?.closest('.next-lanes-row');
    const lines=[...(row?.querySelectorAll('.next-lanes-attention')||[])].map(line=>({
      title:line.querySelector('.next-lanes-attention-goal')?.textContent||'',
      reason:line.querySelector('.next-lanes-attention-reason')?.textContent||'',
    }));
    return lines[0]?.title === ${JSON.stringify(noRoadQuestionGoal?.title)}
      && lines[0]?.reason === 'needs you'
      && lines[1]?.title === ${JSON.stringify(noRoadFirstBlockedGoal?.title)}
      && lines[1]?.reason === ${JSON.stringify(blockedReason(noRoadFirstBlockedGoal))}
      && row?.querySelector('.next-lanes-more')?.textContent === '+1 more';
  })()`, { timeoutMs: ctx.timeoutMs });
  const rootNoRoadAfterGoals = await laneRootEvidence(session, L65_ID);
  check(report, 'E8: L65 root attention prioritizes its preview question, then a blocked goal, with the exact remaining count',
    rootNoRoadAfterGoals?.attention.length === 2
      && rootNoRoadAfterGoals.attention[0].title === noRoadQuestionGoal?.title
      && rootNoRoadAfterGoals.attention[0].reason === 'needs you'
      && rootNoRoadAfterGoals.attention[1].title === noRoadFirstBlockedGoal?.title
      && rootNoRoadAfterGoals.attention[1].reason === blockedReason(noRoadFirstBlockedGoal)
      && rootNoRoadAfterGoals.more === '+1 more',
    { rootNoRoadAfterGoals, noRoadQuestionGoal, noRoadFirstBlockedGoal });

  // Lane reads have no timer; the only next-screen timer is the 2.6 s answer receipt in questions.js.
  const callsBeforeFlush = await session.eval('window.__nextLanes.calls.length');
  await session.eval('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
  const callsAfterFlush = await session.eval('window.__nextLanes.calls.length');
  const afterWaitPreview = await questionEvidence(session, ids[0]);
  const afterWaitOffPreview = await questionEvidence(session, ids[1]);
  check(report, 'E8: a two-frame checkpoint does not invent headings or issue extra reads while reads remain failed',
    callsAfterFlush === callsBeforeFlush && afterWaitPreview?.ownTitle === 'Synthetic preview question' && !afterWaitPreview.linkTitle && afterWaitPreview.retry
      && afterWaitOffPreview?.ownTitle === 'Synthetic off-preview question' && !afterWaitOffPreview.linkTitle && afterWaitOffPreview.retry
      && !afterWaitPreview.isChatLink && !afterWaitOffPreview.isChatLink,
    { afterWaitPreview, afterWaitOffPreview, callsBeforeFlush, callsAfterFlush });

  const noLaneSelector = questionSelector(ids[2]);
  await session.click(`${noLaneSelector} .next-question-option`);
  await session.click(`${noLaneSelector} .next-question-send`);
  await session.waitFor(`document.querySelector('[aria-label="Questions (4)"]')?.dataset.count === '4'`, { timeoutMs: ctx.timeoutMs });
  check(report, 'E8: an unplaced question remains answerable and answering it lowers the count',
    await hasOpenQuestionsControl(session, 4), { answeredQuestionId: ids[2] });
  const storedNoLaneAnswer = await fixtureRequest(ctx.runtime, PRODUCER, { type: 'prompt.status', question_id: ids[2] });
  check(report, 'E8: the isolated daemon stores the selected answer for the still-unplaced question',
    storedNoLaneAnswer?.ok === true && ['answered', 'consumed'].includes(storedNoLaneAnswer.question?.state)
      && storedNoLaneAnswer.question?.answer?.selections?.[0] === 'keep',
    { questionId: ids[2], state: storedNoLaneAnswer?.question?.state, answer: storedNoLaneAnswer?.question?.answer });

  await closeQuestionList(session);
  const callsBeforeRootRetry = await laneCallEvidence(session, L11_ID);
  await session.eval(`window.__nextLanes.failures.delete(${JSON.stringify(L11_ID)}); true`);
  const clickedRootRetry = await session.eval(`(() => {
    const open = document.querySelector('.next-lanes-row-open[data-lane-id=${JSON.stringify(L11_ID)}]');
    const row = open?.closest('.next-lanes-row');
    const retry = row?.querySelector('.next-lanes-retry[data-read-kind="goals"]');
    if (retry) retry.click();
    return !!retry;
  })()`);
  if (!clickedRootRetry) throw new Error('the L11 root goals Retry button was not available');
  await session.waitFor(`window.__nextLanes.calls.filter(call => call.laneId === ${JSON.stringify(L11_ID)}
    && call.revision === ${JSON.stringify(REVISION_A)} && Number(call.options.membersLimit) === 1).length
    > ${callsBeforeRootRetry.filter(call => call.membersLimit === 1).length}`, { timeoutMs: ctx.timeoutMs });
  await session.waitFor(`(() => {
    const row=document.querySelector('.next-lanes-row-open[data-lane-id=${JSON.stringify(L11_ID)}]')?.closest('.next-lanes-row');
    const lines=[...(row?.querySelectorAll('.next-lanes-attention')||[])].map(line=>({
      title:line.querySelector('.next-lanes-attention-goal')?.textContent||'',
      reason:line.querySelector('.next-lanes-attention-reason')?.textContent||'',
    }));
    return lines.length === 2
      && lines[0]?.title === ${JSON.stringify(previewGoal.title)} && lines[0]?.reason === 'needs you'
      && lines[1]?.title === ${JSON.stringify(l11.goals.find(goal => goal.title === 'Fixture goal 1')?.title)}
      && lines[1]?.reason === ${JSON.stringify(blockedReason(l11.goals.find(goal => goal.title === 'Fixture goal 1')))}
      && row?.querySelector('.next-lanes-more')?.textContent === '+8 more';
  })()`, { timeoutMs: ctx.timeoutMs });
  const rootAfterRetry = await laneRootEvidence(session, L11_ID);
  const callsAfterRootRetry = await laneCallEvidence(session, L11_ID);
  check(report, 'E8: retrying the root row reveals its goal lines and the exact remaining count',
    rootAfterRetry?.attention.length === 2 && rootAfterRetry.more === '+8 more'
      && rootAfterRetry.attention[0]?.title === previewGoal.title
      && rootAfterRetry.attention[0]?.reason === 'needs you'
      && rootAfterRetry.attention[1]?.title === l11.goals.find(goal => goal.title === 'Fixture goal 1')?.title
      && rootAfterRetry.attention[1]?.reason === blockedReason(l11.goals.find(goal => goal.title === 'Fixture goal 1')),
    rootAfterRetry);

  await openQuestionList(ctx);
  const offPreviewSelector = questionSelector(ids[1]);
  await session.waitFor(`!!document.querySelector(${JSON.stringify(`${offPreviewSelector} .next-question-retry`)})`, { timeoutMs: ctx.timeoutMs });
  const callsBeforeQuestionRetry = callsAfterRootRetry.length;
  await session.click(`${offPreviewSelector} .next-question-retry`);
  await session.waitFor(`window.__nextLanes.calls.filter(call => call.laneId === ${JSON.stringify(L11_ID)}).length > ${callsBeforeQuestionRetry}`, { timeoutMs: ctx.timeoutMs });
  await session.waitFor(`(() => {
    const first=document.querySelector(${JSON.stringify(questionSelector(ids[0]))});
    const second=document.querySelector(${JSON.stringify(questionSelector(ids[1]))});
    return first?.querySelector('.next-question-goal-title')?.textContent === ${JSON.stringify(previewGoal.title)}
      && second?.querySelector('.next-question-goal-title')?.textContent === ${JSON.stringify(offPreviewGoal.title)};
  })()`, { timeoutMs: ctx.timeoutMs });
  const expectedQuestionGoalOrder = [previewGoal, offPreviewGoal].sort((a, b) => a.pos - b.pos).map(goal => goal.title);
  await session.waitFor(`(() => {
    const row=document.querySelector('.next-lanes-row-open[data-lane-id=${JSON.stringify(L11_ID)}]')?.closest('.next-lanes-row');
    const needs=[...(row?.querySelectorAll('.next-lanes-attention')||[])]
      .filter(line => line.querySelector('.next-lanes-attention-reason')?.textContent === 'needs you')
      .map(line => line.querySelector('.next-lanes-attention-goal')?.textContent || '');
    return needs.slice(0, 2).join('|') === ${JSON.stringify(expectedQuestionGoalOrder.join('|'))}
      && row?.querySelector('.next-lanes-more')?.textContent === '+8 more';
  })()`, { timeoutMs: ctx.timeoutMs });
  const previewPlaced = await questionEvidence(session, ids[0]);
  const offPreviewPlaced = await questionEvidence(session, ids[1]);
  const callsAfterQuestionRetry = await laneCallEvidence(session, L11_ID);
  check(report, 'E8: the question Retry issues the failed members read for the current lane revision',
    callsAfterQuestionRetry.slice(callsBeforeQuestionRetry).some(call => call.revision === REVISION_A && call.membersLimit === 32),
    { callsBeforeQuestionRetry, callsAfterQuestionRetry });
  check(report, 'E8: Retry on an L11 question finds both specs and restores lane plus goal headings',
    [previewPlaced, offPreviewPlaced].every(item => item?.linkTitle?.includes('Fixture lane eleven') && item.goalTitle)
      && previewPlaced.goalTitle === previewGoal.title && offPreviewPlaced.goalTitle === offPreviewGoal.title,
    { previewPlaced, offPreviewPlaced });

  const rootWithQuestions = await laneRootEvidence(session, L11_ID);
  const needsYouTitles = rootWithQuestions?.attention.filter(line => line.reason === 'needs you').map(line => line.title) || [];
  check(report, 'E8: recovered L11 root lines prioritize both question goals',
    needsYouTitles.slice(0, 2).join('|') === expectedQuestionGoalOrder.join('|')
      && rootWithQuestions.more === '+8 more',
    rootWithQuestions);

  await session.click(`${questionSelector(ids[1])} .next-question-heading-link`);
  await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level === 'spec'", { timeoutMs: ctx.timeoutMs });
  await selectQuestionCard(session, ids[1], ctx.timeoutMs);
  check(report, 'E8: the recovered question card appears at spec level',
    await session.eval(`document.querySelector('.next-lanes-scroll .next-question-card')?.dataset.questionId === ${JSON.stringify(ids[1])}`));
  await session.click('.next-lanes-back');
  await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level === 'goal'", { timeoutMs: ctx.timeoutMs });
  await selectQuestionCard(session, ids[1], ctx.timeoutMs);
  check(report, 'E8: the recovered question card appears at goal level',
    await session.eval(`document.querySelector('.next-lanes-scroll .next-question-card')?.dataset.questionId === ${JSON.stringify(ids[1])}`));
  await session.click('.next-lanes-back');
  await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level === 'lane'", { timeoutMs: ctx.timeoutMs });
  await selectQuestionCard(session, ids[1], ctx.timeoutMs);
  check(report, 'E8: the recovered question card appears at lane level',
    await session.eval(`document.querySelector('.next-lanes-scroll .next-question-card')?.dataset.questionId === ${JSON.stringify(ids[1])}`));

  await session.click('.next-lanes-back');
  await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level === 'all'", { timeoutMs: ctx.timeoutMs });
  await closeQuestionList(session);
  await session.click(`.next-lanes-row-open[data-lane-id=${JSON.stringify(noRoad.inventory.lane_id)}]`);
  await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level === 'lane'", { timeoutMs: ctx.timeoutMs });
  await session.waitFor(`window.__nextLanes.pending.some(item => item.laneId === ${JSON.stringify(noRoad.inventory.lane_id)} && Number(item.options.membersLimit) === 32 && item.options.goalId == null && item.revision === ${JSON.stringify(noRoad.detailRevision)})`, { timeoutMs: ctx.timeoutMs });
  const noRoadBeforeRead = await laneDetailStops(session);
  const noRoadSentIds = noRoad.inventory.goals.slice(0, 8).map(goal => goal.goal_id);
  check(report, 'E8: L65 omits raw road and position keys, and its eight sent stops retain daemon order before detail loads',
    !Object.hasOwn(noRoad.inventory, 'goal_road')
      && !Object.hasOwn(noRoad.projection, 'goal_road')
      && noRoad.inventory.goals.every(goal => !Object.hasOwn(goal, 'pos'))
      && noRoad.goals.every(goal => !Object.hasOwn(goal, 'pos'))
      && noRoadBeforeRead.level === 'lane'
      && noRoadBeforeRead.stops.filter(stop => stop.goalId).length === 8
      && noRoadBeforeRead.stops.filter(stop => stop.goalId).map(stop => stop.goalId).join('|') === noRoadSentIds.join('|')
      && noRoadBeforeRead.stops.filter(stop => stop.goalId).every(stop => stop.number === null)
      && noRoadBeforeRead.stops[8]?.defaultGoal === true
      && noRoadBeforeRead.more === '+57 more',
    { laneId: noRoad.inventory.lane_id, noRoadBeforeRead, noRoadSentIds });

  const noRoadRelease = await session.eval(`window.__nextLanesReleaseMatchingAndFlush({ laneId: ${JSON.stringify(noRoad.inventory.lane_id)}, goalId: null, membersLimit: 32, revision: ${JSON.stringify(noRoad.detailRevision)} })`);
  check(report, 'E8: the L65 full-members reply is released and processed at its requested revision',
    noRoadRelease?.released === true && noRoadRelease.status === 'resolved'
      && noRoadRelease.laneId === noRoad.inventory.lane_id
      && noRoadRelease.requestedRevision === noRoad.detailRevision
      && noRoadRelease.answeredRevision === noRoad.detailRevision && noRoadRelease.frameProcessed === true,
    flushReceipt(noRoadRelease));
  await session.waitFor(`document.querySelectorAll('.next-lanes-scroll .next-lanes-stop-open[data-goal-id]').length === 65`, { timeoutMs: ctx.timeoutMs });
  const noRoadAfterRead = await laneDetailStops(session);
  const allNoRoadIds = noRoad.goals.map(goal => goal.goal_id);
  const zuluIndex = noRoadAfterRead.stops.findIndex(stop => stop.title.includes('Zulu'));
  const alphaIndex = noRoadAfterRead.stops.findIndex(stop => stop.title.includes('Alpha'));
  check(report, 'E8: the full L65 reply preserves all 65 unnumbered stops, equal-rank daemon order, and the no-goal stop',
    noRoadAfterRead.stops.filter(stop => stop.goalId).map(stop => stop.goalId).join('|') === allNoRoadIds.join('|')
      && noRoadAfterRead.stops.length === 66
      && noRoadAfterRead.stops.every(stop => stop.number === null)
      && zuluIndex >= 0 && alphaIndex > zuluIndex
      && noRoadAfterRead.stops[65]?.defaultGoal === true
      && noRoadAfterRead.stops[65]?.title === noRoad.inventory.title
      && noRoadAfterRead.more === null,
    { noRoadAfterRead, zuluIndex, alphaIndex, allNoRoadIds });
}

function createE9Fixture() {
  const parkedKey = 'e9-parked-ninth';
  const ordinaryKey = 'e9-ordinary-preview';
  const lateKey = 'e9-late-member';
  const excludedKey = 'e9-truncated-hidden-lane';
  const movedKey = 'e9-revision-moved';
  const parkedQuestionId = questionIdForKey(parkedKey);
  const movedQuestionId = questionIdForKey(movedKey);
  const ordinaryQuestionId = questionIdForKey(ordinaryKey);
  const lateQuestionId = questionIdForKey(lateKey);
  const hiddenQuestionId = questionIdForKey(excludedKey);
  const parkedBase = makeParkedLane({ questionIds: [parkedQuestionId] });
  const parked = staleQuestionPreview(parkedBase, parkedBase.questionSpecId);
  const ordinaryBase = makeOrdinaryLane({ laneId: ORDINARY_A_ID, title: 'Fixture ordinary lane' });
  const ordinaryMembers = ordinaryBase.members.map((member, index) => index === 0
    ? { ...member, open_questions: { count: 1, first_question_id: ordinaryQuestionId } }
    : member);
  const ordinary = makeOrdinaryLane({
    laneId: ORDINARY_A_ID,
    title: 'Fixture ordinary lane',
    goalId: ordinaryBase.goals[0].goal_id,
    goalTitle: ordinaryBase.goals[0].title,
    members: ordinaryMembers,
  });
  const lateA = makeOrdinaryLane({
    laneId: ORDINARY_B_ID,
    title: 'Fixture late-member lane',
    revision: REVISION_A,
    memberCount: 8,
  });
  const { l11A, l11B, movedLane, movedSpecId } = e9MovedRevisionFixture(movedQuestionId);
  const movedLaneId = movedLane.inventory.lane_id;
  const hiddenSpecId = 'spec_gate__next_lanes_hidden_fixture';
  const hidden = makeOrdinaryLane({
    laneId: fixtureLaneId('z-omitted-paused'),
    title: 'Fixture omitted lane',
    memberCount: 1,
    state: 'paused',
    activity: null,
    questionSpecIds: [hiddenSpecId],
    questionIdsBySpec: { [hiddenSpecId]: [hiddenQuestionId] },
  });
  hidden.inventory.updated_at = '2026-11-19T12:00:00Z';
  hidden.inventory.state_at = hidden.inventory.updated_at;
  const lateSpecId = 'spec_gate__next_lanes_late_question';
  const lateGoalId = lateA.goals[0].goal_id;
  const lateQuestionMember = makeMember(lateSpecId, lateGoalId, {
    title: 'Fixture late-added spec',
    open_questions: { count: 1, first_question_id: lateQuestionId },
  });
  const lateB = makeOrdinaryLane({
    laneId: ORDINARY_B_ID,
    title: 'Fixture late-member lane',
    revision: REVISION_B,
    goalId: lateGoalId,
    members: [
      ...lateA.members.slice(0, 7),
      lateQuestionMember,
      lateA.members[7],
    ],
  });
  const versions = laneDetailVersions([parked, ordinary, lateA, lateB, hidden, l11A, l11B, movedLane]);

  const questions = [
    { key: parkedKey, specId: parked.questionSpecId, title: 'Synthetic parked ninth-member question' },
    { key: ordinaryKey, specId: ordinary.members[0].spec_id, title: 'Synthetic ordinary preview question' },
    { key: lateKey, specId: lateSpecId, title: 'Synthetic late-member question' },
    { key: excludedKey, specId: hiddenSpecId, title: 'Synthetic truncated-hidden question', producerStreamId: PRODUCER },
    { key: movedKey, specId: movedSpecId, title: 'Synthetic revision-moved question' },
  ];
  const bridge = {
    versions,
    holdRules: [
      { laneId: PARKED_ID, goalId: null, membersLimit: 32, remaining: 1 },
      { laneId: movedLaneId, goalId: null, membersLimit: 32, remaining: 1 },
      { laneId: L11_ID, goalId: null, membersLimit: 32, remaining: 2 },
      { laneId: L11_ID, goalId: l11A.members.find(member => member.spec_id === movedSpecId).goal_id,
        revision: REVISION_B, membersLimit: 32, remaining: 1 },
    ],
  };
  const ids = questions.map(question => questionIdForKey(question.key));
  return {
    questions, ids, bridge, firstFrame: truncatedInventoryFrame([parked, ordinary, lateA], hidden),
    parked, ordinary, lateB, hidden, l11A, l11B, movedLane, movedSpecId, lateSpecId, lateGoalId,
  };
}

async function runDataEvidence(ctx, { ownedIds, hooks }) {
  const { session, report } = ctx;
  const fixture = createE9Fixture();
  const { parked, hidden, firstFrame } = fixture;
  const [parkedQuestionId, ordinaryQuestionId] = fixture.ids;
  const parkedQuestionMember = parked.members.find(member => member.spec_id === parked.questionSpecId);
  check(report, 'E9(a): parked fixture omits its ninth question member from the zero-count preview while retaining exact detail attribution',
    parked.members.length === 12
      && parked.members[8]?.spec_id === parked.questionSpecId
      && parkedQuestionMember?.open_questions?.count === 1
      && parkedQuestionMember.open_questions.first_question_id === parkedQuestionId
      && parked.inventory.activity == null
      && parked.inventory.members.length === 8
      && !parked.inventory.members.some(member => member.spec_id === parked.questionSpecId)
      && parked.inventory.members.every(member => Number(member.open_questions?.count || 0) === 0),
    { memberCount: parked.members.length, questionSpecId: parked.questionSpecId,
      detailQuestionMember: parkedQuestionMember, activity: parked.inventory.activity,
      previewSpecIds: parked.inventory.members.map(member => member.spec_id),
      previewQuestionCounts: parked.inventory.members.map(member => member.open_questions?.count || 0) });
  hooks.push(await enterNext(ctx));
  const headingGuard = await startQuestionHeadingObserver(session, fixture.ids);
  check(report, 'E9: continuous question-heading observation is installed before fixture question seeding',
    headingGuard?.installed === true
      && fixture.ids.every(questionId => headingGuard.observedIds.includes(questionId)),
    headingGuard);
  const snapshotBeforeInventory = await session.eval('window.cc.getChatStreamState()');
  const snapshotSessionIds = (snapshotBeforeInventory?.sessions || []).map(row => row?.stream_id).filter(Boolean);
  await session.waitFor(`(window.PentacleChatStore?.getState?.()?.sessions || [])
    .some(row => row?.stream_id === ${JSON.stringify(PRODUCER)})`, { timeoutMs: ctx.timeoutMs });
  const storeSessionIdsBeforeInventory = await session.eval(`(window.PentacleChatStore?.getState?.()?.sessions || [])
    .map(row => row?.stream_id).filter(Boolean)`);
  check(report, 'E9(b): the omitted-lane question producer is a listed chat before the first inventory frame',
    snapshotSessionIds.includes(PRODUCER) && storeSessionIdsBeforeInventory.includes(PRODUCER),
    { producer: PRODUCER, snapshotSessionIds, storeSessionIdsBeforeInventory });
  await session.eval(`window.__nextLanesEmitSourceFrame({
    type: 'chat.stream.snapshot',
    connected: true,
    sessions: ${JSON.stringify(snapshotBeforeInventory?.sessions || [])},
    notifications: [],
    work_lanes: { counts: { open: 65 }, lanes: [] },
  })`);
  await session.eval('window.cc.getChatStreamState()');
  const seeded = [];
  for (const question of fixture.questions) seeded.push(await seedQuestion(ctx, ownedIds, question));
  const ids = seeded.map(question => question.question_id);
  const parkedNotification = await notificationFor(ctx, parkedQuestionId);
  if (parkedNotification?.question?.question_id !== parkedQuestionId) {
    throw new Error('synthetic parked question notification is unavailable for the source-frame preservation check');
  }
  await session.eval(`(() => {
    const emit = window.__nextLanesEmitSourceFrame;
    if (typeof emit !== 'function') throw new Error('synthetic wrapped source-frame seam is unavailable');
    const lanePayload = { type: 'work_lanes.inventory', counts: { open: 0, active: 0, paused: 0, blocked: 0 }, lanes: [] };
    const sessions = window.PentacleChatStore?.getState?.()?.sessions || [];
    emit({ type: 'snapshot', connected: true, sessions, notifications: [${JSON.stringify(parkedNotification)}],
      work_lanes: { counts: { open: 65 }, lanes: [] } });
    emit(lanePayload);
    return true;
  })()`);
  await installLaneBridge(ctx, fixture.bridge);
  const startupEvidence = await laneBridgeEvidence(session);
  const sourceEvents = startupEvidence.sourceEvents || [];
  const directSnapshot = sourceEvents.find(event => event.source === 'getChatStreamState' && event.hadWorkLanes);
  const callbackEvents = sourceEvents.filter(event => event.source === 'onChatStreamFrame' && event.hadWorkLanes);
  const callbackFramesStripped = callbackEvents.some(event => ['stripped', 'withheld'].includes(event.action));
  check(report, 'E9(b): startup direct snapshots and wrapped push frames withhold lane inventory while preserving connection state',
    startupEvidence.frameSequence === 0 && (startupEvidence.acceptedFrames || []).length === 0
      && (startupEvidence.normalizedInventory?.lanes || []).length === 0
      && ['stripped', 'withheld'].includes(directSnapshot?.action)
      && directSnapshot.connectedPresent === true && directSnapshot.connected === true
      && directSnapshot.sessionsPresent === true && directSnapshot.notificationsPresent === false
      && callbackFramesStripped
      && callbackEvents.some(event => event.type === 'chat.stream.snapshot' && event.action === 'stripped'
        && event.connected === true && event.sessionsPresent && event.notificationsPresent)
      && callbackEvents.some(event => event.type === 'snapshot' && event.connectedPresent && event.connected
        && event.sessionsPresent && event.notificationsPresent),
    { frameSequence: startupEvidence.frameSequence, acceptedFrames: startupEvidence.acceptedFrames,
      directSnapshot, callbackEvents, normalizedLaneCount: startupEvidence.normalizedInventory?.lanes?.length });
  const forwardedParkedNotification = await session.eval(`(() => {
    const state = window.PentacleChatStore?.getState?.() || {};
    return (state.notifications || []).some(item => item.question?.question_id === ${JSON.stringify(parkedQuestionId)});
  })()`);
  check(report, 'E9(b): the wrapped connection frame keeps the owned question notification after stripping lane inventory',
    forwardedParkedNotification === true
      && callbackEvents.some(event => event.type === 'snapshot' && event.action === 'stripped'
        && event.connected === true && event.sessionsPresent && event.notificationsPresent),
    { forwardedParkedNotification, callbackEvents });
  await waitForQuestionRows(ctx, seeded);

  const beforeFrame = await Promise.all(ids.map(id => questionEvidence(session, id)));
  check(report, 'E9(b): with the question list delivered and inventory withheld, every spec question keeps its own title',
    beforeFrame.every((item, index) => item?.ownTitle === [
      'Synthetic parked ninth-member question',
      'Synthetic ordinary preview question',
      'Synthetic late-member question',
      'Synthetic truncated-hidden question',
      'Synthetic revision-moved question',
    ][index] && !item.linkTitle && !item.isChatLink && !item.retry),
    beforeFrame);

  const firstFrameReceipt = await emitInventory(session, firstFrame);
  const firstFrameEvidence = await currentFrameEvidence(session);
  const firstBridgeEvidence = await laneBridgeEvidence(session);
  const normalizedParked = normalizedLane(firstBridgeEvidence, PARKED_ID);
  const truncatedAffordance = await session.eval("document.querySelector('.next-lanes-truncated')?.textContent || null");
  check(report, 'E9(b): production-sorted truncation sends all 63 active lanes, then the included paused lane, omitting the later paused lane',
    firstFrameEvidence.truncated === true && firstFrameEvidence.laneCount === 64
      && firstFrameEvidence.counts?.open === 65 && firstFrameEvidence.counts?.active === 63
      && firstFrameEvidence.counts?.paused === 2 && firstFrameEvidence.states.slice(0, 63).every(state => state === 'active')
      && firstFrameEvidence.laneIds[63] === PARKED_ID && !firstFrameEvidence.laneIds.includes(hidden.inventory.lane_id)
      && truncatedAffordance === '+1 more lanes',
    { firstFrameEvidence, truncatedAffordance, omittedLaneId: hidden.inventory.lane_id });
  const acceptedFirstFrame = (firstBridgeEvidence.acceptedFrames || [])
    .find(item => item.frameSequence === firstFrameReceipt.frameSequence);
  check(report, 'E9(b): the first fixture frame is accepted at its injection sequence and normalized from flat raw preview fields',
    firstFrameReceipt.accepted === true && acceptedFirstFrame?.renderFrame === firstFrameReceipt.renderFrame
      && normalizedParked?.progress?.members?.length === 8
      && normalizedParked.progress.members_total === 12,
    { firstFrameReceipt, acceptedFrame: acceptedFirstFrame
      ? { frameSequence: acceptedFirstFrame.frameSequence, flushSequence: acceptedFirstFrame.renderFrame }
      : null, normalizedParked });
  await session.waitFor(`window.__nextLanes.pending.some(item => item.laneId === ${JSON.stringify(PARKED_ID)} && item.revision === ${JSON.stringify(REVISION_A)} && Number(item.options.membersLimit) === 32 && item.options.goalId == null)`, { timeoutMs: ctx.timeoutMs });
  const reconnectEvidence = await reconnectAndReplaceSubscription(ctx);
  await session.waitFor(`(window.PentacleChatStore?.getState?.()?.notifications || [])
    .some(item => item?.question?.question_id === ${JSON.stringify(parkedQuestionId)})`,
  { timeoutMs: ctx.timeoutMs, label: 'owned notification remains in the store after reconnect' });
  const storeAfterReconnect = await session.eval(`(() => {
    const state = window.PentacleChatStore?.getState?.() || {};
    return {
      connected: state.connected === true,
      sessionIds: (state.sessions || []).map(row => row?.stream_id).filter(Boolean),
      notificationQuestionIds: (state.notifications || [])
        .map(item => item?.question?.question_id || item?.question_id || null).filter(Boolean),
    };
  })()`);
  const frameAfterReconnect = await currentFrameEvidence(session);
  const parkedAfterReconnect = await questionEvidence(session, parkedQuestionId);
  const ordinaryAfterReconnect = await questionEvidence(session, ordinaryQuestionId);
  const parkedStillPending = await session.eval(`window.__nextLanes.pending.some(item => item.laneId === ${JSON.stringify(PARKED_ID)}
    && item.revision === ${JSON.stringify(REVISION_A)} && Number(item.options.membersLimit) === 32 && item.options.goalId == null)`);
  check(report, 'E9(b): a real /cc reconnect retains connection, session and owned-notification state',
    reconnectEvidence.after.socketSequence > reconnectEvidence.before.socketSequence
      && reconnectEvidence.after.socketReadyState === 1
      && reconnectEvidence.readback.transportOpen === true
      && reconnectEvidence.readback.snapshotConnected === true
      && reconnectEvidence.readback.snapshotHasWorkLanes === false
      && reconnectEvidence.readback.snapshotSessionIds.includes(PRODUCER)
      && reconnectEvidence.readback.reconnectSnapshotEvents.some(event => event.action === 'stripped'
        && event.connectedPresent && event.connected && event.sessionsPresent && event.notificationsPresent === false)
      && reconnectEvidence.readback.storeNotificationQuestionIds.includes(parkedQuestionId)
      && storeAfterReconnect.connected === true
      && storeAfterReconnect.sessionIds.includes(PRODUCER)
      && storeAfterReconnect.notificationQuestionIds.includes(parkedQuestionId),
    { socketSequence: reconnectEvidence.after.socketSequence,
      connectionOpen: reconnectEvidence.readback.transportOpen,
      snapshotConnected: reconnectEvidence.readback.snapshotConnected,
      snapshotHasWorkLanes: reconnectEvidence.readback.snapshotHasWorkLanes,
      snapshotSessionIds: reconnectEvidence.readback.snapshotSessionIds,
      snapshotNotificationQuestionIds: reconnectEvidence.readback.snapshotNotificationQuestionIds,
      readbackStoreNotificationQuestionIds: reconnectEvidence.readback.storeNotificationQuestionIds,
      reconnectSnapshotEvents: reconnectEvidence.readback.reconnectSnapshotEvents,
      storeAfterReconnect });
  check(report, 'E9(b): replacing the renderer subscription after reconnect replays the owned fixture without advancing its frame sequence',
    reconnectEvidence.replacement.replaced === true
      && reconnectEvidence.replacement.callbackReplaced === true
      && reconnectEvidence.replacement.currentFrameAvailable === true
      && reconnectEvidence.replacement.subscriptionSequence > reconnectEvidence.before.subscriptionSequence
      && reconnectEvidence.replacement.replayedCurrentFrame === true
      && reconnectEvidence.replacement.renderFrame > 0
      && reconnectEvidence.replacement.frameSequence === firstFrameReceipt.frameSequence
      && reconnectEvidence.after.frameSequence === firstFrameReceipt.frameSequence
      && frameAfterReconnect.frameSequence === firstFrameReceipt.frameSequence
      && frameAfterReconnect.truncated === true
      && frameAfterReconnect.laneIds[63] === PARKED_ID
      && parkedStillPending
      && parkedAfterReconnect?.ownTitle === 'Synthetic parked ninth-member question'
      && !parkedAfterReconnect.linkTitle && !parkedAfterReconnect.retry
      && ordinaryAfterReconnect?.linkTitle?.includes('Fixture ordinary lane'),
    { before: reconnectEvidence.before, replacement: reconnectEvidence.replacement, after: reconnectEvidence.after,
      frameAfterReconnect, parkedStillPending, parkedAfterReconnect, ordinaryAfterReconnect });
  await verifyE9Placement(ctx, fixture);
}

async function verifyE9Placement(ctx, fixture) {
  const { session, report } = ctx;
  const { ids, parked, ordinary, lateB, hidden, l11A, l11B, movedLane, movedSpecId, lateSpecId, lateGoalId } = fixture;
  const [parkedQuestionId, , lateQuestionId, , movedQuestionId] = ids;
  const movedLaneId = movedLane.inventory.lane_id;
  const afterFirstFrame = await Promise.all(ids.map(id => questionEvidence(session, id)));
  const producerListedAfterFrame = await session.eval(`(window.PentacleChatStore?.getState?.()?.sessions || [])
    .some(row => row?.stream_id === ${JSON.stringify(PRODUCER)})`);
  check(report, 'E9(b): the omitted-lane question producer remains a listed chat after the truncated frame',
    producerListedAfterFrame === true, { producer: PRODUCER, producerListedAfterFrame });
  check(report, 'E9(b): the truncated frame places only the ordinary preview question before the parked read lands',
    afterFirstFrame[1]?.linkTitle?.includes('Fixture ordinary lane')
      && afterFirstFrame[1]?.goalTitle === ordinary.goals[0].title
      && [0, 2, 3, 4].every(index => afterFirstFrame[index]?.ownTitle && !afterFirstFrame[index].linkTitle && !afterFirstFrame[index].isChatLink),
    afterFirstFrame);
  check(report, 'E9(b): a spec in a lane omitted by the truncated frame stays under its own title with no Retry',
    afterFirstFrame[3]?.ownTitle === 'Synthetic truncated-hidden question'
      && !afterFirstFrame[3].linkTitle && !afterFirstFrame[3].isChatLink && !afterFirstFrame[3].retry,
    afterFirstFrame[3]);
  check(report, 'E9(a): the parked ninth-member question remains its own title while the members read is held',
    afterFirstFrame[0]?.ownTitle === 'Synthetic parked ninth-member question'
      && !afterFirstFrame[0].linkTitle && !afterFirstFrame[0].isChatLink && !afterFirstFrame[0].retry,
    afterFirstFrame[0]);

  const releasedParked = await session.eval(`window.__nextLanesReleaseMatchingAndFlush({ laneId: ${JSON.stringify(PARKED_ID)}, goalId: null, membersLimit: 32, revision: ${JSON.stringify(REVISION_A)} })`);
  check(report, 'E9(a): the held parked members page is released and flushed at revision A',
    releasedParked?.released === true && releasedParked.status === 'resolved'
      && releasedParked.laneId === PARKED_ID
      && releasedParked.requestedRevision === REVISION_A && releasedParked.answeredRevision === REVISION_A
      && releasedParked.frameProcessed === true,
    flushReceipt(releasedParked));
  await session.waitFor(`!!document.querySelector(${JSON.stringify(`${questionSelector(parkedQuestionId)} .next-question-heading-link .next-question-goal-title`)})`, { timeoutMs: ctx.timeoutMs });
  const afterParkedRead = await questionEvidence(session, parkedQuestionId);
  check(report, 'E9(a): after the parked lane members read, the ninth-member question is headed by its lane and goal',
    afterParkedRead?.linkTitle?.includes('Fixture parked lane')
      && afterParkedRead.goalTitle === parked.goals[0].title
      && !afterParkedRead.isChatLink,
    afterParkedRead);

  const laterFrame = truncatedInventoryFrame([parked, ordinary, lateB], hidden);
  const laterFrameReceipt = await emitInventory(session, laterFrame);
  await session.waitFor(`!!document.querySelector(${JSON.stringify(`${questionSelector(ids[2])} .next-question-heading-link .next-question-goal-title`)})`, { timeoutMs: ctx.timeoutMs });
  const laterFrameEvidence = await laneBridgeEvidence(session);
  const normalizedLateB = normalizedLane(laterFrameEvidence, ORDINARY_B_ID);
  const normalizedLateQuestionMember = normalizedLateB?.progress?.members?.find(member => member.spec_id === lateSpecId);
  const latePlaced = await questionEvidence(session, ids[2]);
  const movedHeld = await questionEvidence(session, ids[4]);
  check(report, 'E9(c): the injected revision-B preview contains the exact newly added member and question attribution',
    normalizedLateQuestionMember?.goal_id === lateGoalId
      && normalizedLateQuestionMember?.open_questions?.count === 1
      && normalizedLateQuestionMember?.open_questions?.first_question_id === lateQuestionId
      && normalizedLateB?.progress?.members_total === 9
      && laterFrameReceipt.accepted === true,
    { laterFrameReceipt, normalizedLateQuestionMember, membersTotal: normalizedLateB?.progress?.members_total });
  check(report, 'E9(c): the question delivered before membership takes the ordinary lane heading when revision B is injected',
    latePlaced?.linkTitle?.includes('Fixture late-member lane')
      && latePlaced.goalTitle === lateB.goals[0].title && !latePlaced.isChatLink,
    latePlaced);
  check(report, 'E9(c): the revision-moved question stays under its own title until its lane enters the frame',
    movedHeld?.ownTitle === 'Synthetic revision-moved question' && !movedHeld.linkTitle && !movedHeld.isChatLink && !movedHeld.retry,
    movedHeld);

  const frameA = truncatedInventoryFrame([parked, ordinary, lateB, l11A], hidden);
  const frameAReceipt = await emitInventory(session, frameA);
  await session.waitFor(`window.__nextLanes.pending.some(item => item.laneId === ${JSON.stringify(L11_ID)} && item.revision === ${JSON.stringify(REVISION_A)} && Number(item.options.membersLimit) === 32 && item.options.goalId == null)`, { timeoutMs: ctx.timeoutMs });
  const evidenceAtA = await laneBridgeEvidence(session);
  const normalizedA = normalizedLane(evidenceAtA, L11_ID);
  const previewA = normalizedA?.progress?.members || [];
  const l11AQuestionMember = l11A.members.find(member => member.spec_id === movedSpecId);
  const movedAtA = await questionEvidence(session, movedQuestionId);
  check(report, 'E9(d): revision A retains its complete capped preview while its stale question attribution stays only in detail',
    Number(l11A.inventory.members_total) > l11A.inventory.members.length
      && !l11A.inventory.members.some(member => member.spec_id === movedSpecId)
      && previewA.map(member => member.spec_id).join('|') === l11A.inventory.members.map(member => member.spec_id).join('|')
      && previewA.length === 8 && previewA[0]?.open_questions?.count === 0
      && previewA[0]?.open_questions?.first_question_id == null
      && l11AQuestionMember?.open_questions?.count === 1,
    { rawPreview: l11A.inventory.members.map(member => member.spec_id),
      normalizedPreview: previewA, membersTotal: l11A.inventory.members_total, detailMember: l11AQuestionMember });
  check(report, 'E9(d): the A renderer checkpoint is accepted and the moved question stays unplaced immediately before B',
    frameAReceipt.accepted === true
      && normalizedA?.detail_revision === REVISION_A
      && evidenceAtA.frameSequence === frameAReceipt.frameSequence
      && (evidenceAtA.acceptedFrames || []).some(item => item.frameSequence === frameAReceipt.frameSequence
        && normalizedLane(item, L11_ID)?.detail_revision === REVISION_A)
      && movedAtA?.ownTitle === 'Synthetic revision-moved question' && !movedAtA.linkTitle && !movedAtA.isChatLink && !movedAtA.retry,
    { frameAReceipt, frameSequence: evidenceAtA.frameSequence, flushSequence: evidenceAtA.renderFrame,
      normalizedRevision: normalizedA?.detail_revision, movedAtA });
  const callsAtA = await session.eval(`window.__nextLanes.calls.filter(call => call.laneId === ${JSON.stringify(L11_ID)} && Number(call.options.membersLimit) === 32).map(call => call.revision)`);
  check(report, 'E9(d): the L11 members read starts at revision A and is held',
    callsAtA.includes(REVISION_A), { callsAtA });

  const formerGoalId = l11AQuestionMember?.goal_id || null;
  const movedLaneQuestionMember = movedLane.members.find(member => member.spec_id === movedSpecId);
  const movedMemberFormerGoalInB = l11B.members.some(member => member.spec_id === movedSpecId && member.goal_id === formerGoalId);
  check(report, 'E9(d): before B is emitted, the moved member is absent from both previews and retained only in destination detail',
    Number(movedLane.inventory.members_total) > movedLane.inventory.members.length
      && !movedLane.inventory.members.some(member => member.spec_id === movedSpecId)
      && movedLane.inventory.members.length === 8
      && movedLane.inventory.members.every(member => Number(member.open_questions?.count || 0) === 0
        && member.open_questions?.first_question_id == null)
      && movedLaneQuestionMember?.open_questions?.count === 1
      && movedLaneQuestionMember?.open_questions?.first_question_id === movedQuestionId
      && movedLaneQuestionMember?.goal_id === movedLane.goals[0]?.goal_id
      && movedLaneQuestionMember?.goal_id !== formerGoalId
      && !l11B.inventory.members.some(member => member.spec_id === movedSpecId)
      && !l11B.members.some(member => member.spec_id === movedSpecId)
      && !movedMemberFormerGoalInB,
    { movedSpecId, formerGoalId, destinationGoalId: movedLane.goals[0]?.goal_id,
      rawPreviewIds: movedLane.inventory.members.map(member => member.spec_id),
      destinationMember: movedLaneQuestionMember, l11BPreviewIds: l11B.inventory.members.map(member => member.spec_id) });

  const frameB = truncatedInventoryFrame([parked, ordinary, lateB, l11B, movedLane], hidden);
  const frameBReceipt = await emitInventory(session, frameB);
  await session.waitFor(`window.__nextLanes.calls.some(call => call.laneId === ${JSON.stringify(L11_ID)} && Number(call.options.membersLimit) === 32 && call.options.goalId == null && call.revision === ${JSON.stringify(REVISION_B)})`, { timeoutMs: ctx.timeoutMs });
  await session.waitFor(`window.__nextLanes.pending.some(item => item.laneId === ${JSON.stringify(L11_ID)} && item.revision === ${JSON.stringify(REVISION_B)} && Number(item.options.membersLimit) === 32 && item.options.goalId == null)`, { timeoutMs: ctx.timeoutMs });
  await session.waitFor(`window.__nextLanes.pending.some(item => item.laneId === ${JSON.stringify(movedLaneId)} && item.revision === ${JSON.stringify(REVISION_B)} && Number(item.options.membersLimit) === 32 && item.options.goalId == null)`, { timeoutMs: ctx.timeoutMs });
  await session.waitFor(`(() => {
    const row=document.querySelector('.next-lanes-row-open[data-lane-id=${JSON.stringify(L11_ID)}]')?.closest('.next-lanes-row');
    return !!row && row.querySelectorAll('.next-lanes-attention-goal').length === 0
      && !row.querySelector('.next-lanes-more');
  })()`, { timeoutMs: ctx.timeoutMs });

  const rootAtB = await laneRootEvidence(session, L11_ID);
  const movedBeforeStaleRelease = await questionEvidence(session, movedQuestionId);
  const evidenceAtB = await laneBridgeEvidence(session);
  const normalizedB = normalizedLane(evidenceAtB, L11_ID);
  const movedLanePreviewAtB = normalizedLane(evidenceAtB, movedLaneId)?.progress?.members || [];
  const callsAtB = await session.eval(`window.__nextLanes.calls.filter(call => call.laneId === ${JSON.stringify(L11_ID)} && Number(call.options.membersLimit) === 32).map(call => call.revision)`);
  const allCallsAtB = await laneCallEvidence(session, L11_ID);
  const goalCallsAtB = allCallsAtB.filter(call => call.membersLimit === 1);
  const bGoalsById = new Map(l11B.goals.map(goal => [goal.goal_id, goal]));
  const firstAGoal = l11A.goals[0];
  const secondAGoal = l11A.goals[1];
  const positionsSwapped = bGoalsById.get(firstAGoal.goal_id)?.pos === secondAGoal.pos
    && bGoalsById.get(secondAGoal.goal_id)?.pos === firstAGoal.pos
    && bGoalsById.get(firstAGoal.goal_id)?.rank === secondAGoal.rank
    && bGoalsById.get(secondAGoal.goal_id)?.rank === firstAGoal.rank;
  check(report, 'E9(d): revision B swaps goal positions, starts its members read, and has no root lines while that read is held',
    positionsSwapped && rootAtB?.attention.length === 0 && rootAtB.more === null
      && movedBeforeStaleRelease?.ownTitle === 'Synthetic revision-moved question'
      && !movedBeforeStaleRelease.isChatLink && !movedBeforeStaleRelease.retry
      && callsAtB.includes(REVISION_A) && callsAtB.includes(REVISION_B)
      && revisionRequestsCoalesce(allCallsAtB, L11_ID, REVISION_A, REVISION_B)
      && frameBReceipt.accepted === true && evidenceAtB.frameSequence === frameBReceipt.frameSequence
      && normalizedB?.detail_revision === REVISION_B
      && (evidenceAtB.acceptedFrames || []).some(item => item.frameSequence === frameBReceipt.frameSequence
        && normalizedLane(item, L11_ID)?.detail_revision === REVISION_B),
    { rootAtB, callsAtB, goalCallsAtB, movedBeforeStaleRelease, positionsSwapped, frameBReceipt,
      frameSequence: evidenceAtB.frameSequence, flushSequence: evidenceAtB.renderFrame,
      A: [firstAGoal.goal_id, firstAGoal.pos, firstAGoal.rank, secondAGoal.goal_id, secondAGoal.pos, secondAGoal.rank],
      B: [bGoalsById.get(firstAGoal.goal_id), bGoalsById.get(secondAGoal.goal_id)] });
  check(report, 'E9(d): the normalized B frame preserves the stale destination preview while its detail retains the moved question',
    Number(movedLane.inventory.members_total) > movedLane.inventory.members.length
      && !movedLane.inventory.members.some(member => member.spec_id === movedSpecId)
      && movedLanePreviewAtB.map(member => member.spec_id).join('|') === movedLane.inventory.members.map(member => member.spec_id).join('|')
      && !movedLanePreviewAtB.some(member => member.spec_id === movedSpecId)
      && movedLanePreviewAtB.every(member => Number(member.open_questions?.count || 0) === 0
        && member.open_questions?.first_question_id == null)
      && movedLaneQuestionMember?.open_questions?.count === 1
      && movedLaneQuestionMember?.goal_id === movedLane.goals[0]?.goal_id,
    { preview: movedLanePreviewAtB, rawPreviewIds: movedLane.inventory.members.map(member => member.spec_id),
      membersTotal: movedLane.inventory.members_total, detailMember: movedLaneQuestionMember });

  await closeQuestionList(session);
  await session.click(`.next-lanes-row-open[data-lane-id=${JSON.stringify(L11_ID)}]`);
  await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level === 'lane'", { timeoutMs: ctx.timeoutMs });
  await session.waitFor("document.querySelectorAll('.next-lanes-scroll .next-lanes-stop-open[data-goal-id]').length === 8", { timeoutMs: ctx.timeoutMs });
  const lanePreviewAtB = await laneDetailStops(session);
  const visibleBIds = l11B.inventory.goals.map(goal => goal.goal_id);
  check(report, 'E9(d): while B membership is held, lane detail shows its exact sent goals and +3',
    lanePreviewAtB.stops.map(stop => stop.goalId).join('|') === visibleBIds.join('|')
      && lanePreviewAtB.more === '+3 more',
    { lanePreviewAtB, visibleBIds });
  await session.click('.next-lanes-back');
  await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level === 'all'", { timeoutMs: ctx.timeoutMs });

  // Each lane, stop or Back click can dismiss the list. Reopen the exact row
  // before delivering a held reply, and keep the observer on it through the flush.
  async function releaseObservedPage(label, laneId, revision, goalId = null) {
    const match = { laneId, revision, goalId, membersLimit: 32 };
    const ownTitle = 'Synthetic revision-moved question';
    await openQuestionList(ctx);
    const headingSelector = `.questions-popover:not([hidden]) ${questionSelector(movedQuestionId)} .next-question-list-heading`;
    await session.waitFor(`!!document.querySelector(${JSON.stringify(headingSelector)})`, { timeoutMs: ctx.timeoutMs });
    await session.eval(`document.querySelector(${JSON.stringify(headingSelector)}).scrollIntoView({ block: 'nearest' })`);
    const before = await questionEvidence(session, movedQuestionId);
    check(report, `E9(d): moved question has its own title before ${label}`,
      before?.visible && before.ownTitle === ownTitle && !before.linkTitle && !before.isChatLink && !before.retry,
      { before, match });
    const start = await session.eval('window.__nextLanesQuestionHeadingGuard.snapshot().length');
    const receipt = await session.eval(`window.__nextLanesReleaseMatchingAndFlush(${JSON.stringify(match)})`);
    const after = await questionEvidence(session, movedQuestionId);
    const changes = await session.eval(`window.__nextLanesQuestionHeadingGuard.snapshot().slice(${start})
      .filter(item => item.questionId === ${JSON.stringify(movedQuestionId)} && item.surface === 'list')`);
    const destination = laneId === movedLaneId;
    const expectedDestination = after?.goalTitle === movedLane.goals[0].title && after.laneTitle === movedLane.inventory.title;
    check(report, `E9(d): the moved question stays observable through ${label} with its expected heading`,
      after?.visible && !after.isChatLink && !after.retry
        && (destination ? expectedDestination : after.ownTitle === ownTitle && !after.linkTitle)
        && changes.every(item => item.present && item.visible && !item.chatLink && !item.retry
          && ((item.ownTitle === ownTitle && !item.linkText) || (destination && item.linkText === after.linkTitle))),
      { before, after, changes, match, receipt });
    return receipt;
  }

  const evidenceBeforeStaleRelease = await laneBridgeEvidence(session);
  const callsBeforeStaleRelease = (await laneCallEvidence(session, L11_ID)).filter(call => call.membersLimit === 32).length;
  const destinationPendingBeforeStaleRelease = await session.eval(`window.__nextLanes.pending.some(item => item.laneId === ${JSON.stringify(movedLaneId)} && item.revision === ${JSON.stringify(REVISION_B)} && Number(item.options.membersLimit) === 32 && item.options.goalId == null)`);
  check(report, 'E9(d): the stale-release checkpoint begins on the accepted B frame with both revision reads pending',
    evidenceBeforeStaleRelease.frameSequence === frameBReceipt.frameSequence
      && normalizedLane(evidenceBeforeStaleRelease, L11_ID)?.detail_revision === REVISION_B
      && destinationPendingBeforeStaleRelease
      && await session.eval(`window.__nextLanes.pending.some(item => item.laneId === ${JSON.stringify(L11_ID)} && item.revision === ${JSON.stringify(REVISION_A)} && Number(item.options.membersLimit) === 32 && item.options.goalId == null)
        && window.__nextLanes.pending.some(item => item.laneId === ${JSON.stringify(L11_ID)} && item.revision === ${JSON.stringify(REVISION_B)} && Number(item.options.membersLimit) === 32 && item.options.goalId == null)
        && !window.__nextLanes.pending.some(item => item.laneId === ${JSON.stringify(L11_ID)} && item.revision === ${JSON.stringify(REVISION_B)} && Number(item.options.membersLimit) === 1 && item.options.goalId == null)`),
    { frameSequence: evidenceBeforeStaleRelease.frameSequence, flushSequence: evidenceBeforeStaleRelease.renderFrame,
      callsBeforeStaleRelease, destinationPendingBeforeStaleRelease, rootAtB });

  // The bridge's renderFrame is a two-rAF flush counter; following DOM reads below establish visible state.
  const staleRelease = await releaseObservedPage('the stale A release', L11_ID, REVISION_A);
  check(report, 'E9(d): the held A page resolves with its requested and answered revision and reaches the frame checkpoint',
    staleRelease?.released === true && staleRelease.status === 'resolved'
      && staleRelease.laneId === L11_ID
      && staleRelease.requestedRevision === REVISION_A && staleRelease.answeredRevision === REVISION_A
      && staleRelease.frameProcessed === true
      && staleRelease.frameSequence === frameBReceipt.frameSequence
      && staleRelease.frameSequenceBefore === frameBReceipt.frameSequence
      && staleRelease.frameSequenceAfter === frameBReceipt.frameSequence
      && staleRelease.renderFrame > evidenceBeforeStaleRelease.renderFrame,
    flushReceipt(staleRelease));
  const evidenceAfterStaleRelease = await laneBridgeEvidence(session);
  const frameAfterStaleRelease = await currentFrameEvidence(session);
  const callsAfterStaleRelease = await laneCallEvidence(session, L11_ID);
  const memberCallsAfterStaleRelease = callsAfterStaleRelease.filter(call => call.membersLimit === 32);
  const bStillPending = await session.eval(`window.__nextLanes.pending.some(item => item.laneId === ${JSON.stringify(L11_ID)} && item.revision === ${JSON.stringify(REVISION_B)} && Number(item.options.membersLimit) === 32 && item.options.goalId == null)`);
  const destinationStillPendingAfterStaleRelease = await session.eval(`window.__nextLanes.pending.some(item => item.laneId === ${JSON.stringify(movedLaneId)} && item.revision === ${JSON.stringify(REVISION_B)} && Number(item.options.membersLimit) === 32 && item.options.goalId == null)`);
  const rootAfterStaleRelease = await laneRootEvidence(session, L11_ID);
  const movedAfterStaleRelease = await questionEvidence(session, movedQuestionId);
  const openCountAfterStale = await session.eval("document.querySelector('[aria-label^=Questions]')?.dataset.count");
  check(report, 'E9(d): releasing stale A does not change the renderer accepted frame sequence, preview B, or moved-question title',
    evidenceAfterStaleRelease.frameSequence === evidenceBeforeStaleRelease.frameSequence
      && evidenceAfterStaleRelease.frameSequence === staleRelease.frameSequence
      && evidenceAfterStaleRelease.renderFrame === staleRelease.renderFrame
      && normalizedLane(evidenceAfterStaleRelease, L11_ID)?.detail_revision === REVISION_B
      && openCountAfterStale === String(ids.length)
      && movedAfterStaleRelease?.ownTitle === 'Synthetic revision-moved question'
      && !movedAfterStaleRelease.linkTitle && !movedAfterStaleRelease.isChatLink && !movedAfterStaleRelease.retry,
    { staleRelease, evidenceBeforeStaleRelease: {
      frameSequence: evidenceBeforeStaleRelease.frameSequence, flushSequence: evidenceBeforeStaleRelease.renderFrame,
    }, evidenceAfterStaleRelease: {
      frameSequence: evidenceAfterStaleRelease.frameSequence, flushSequence: evidenceAfterStaleRelease.renderFrame,
      l11Revision: normalizedLane(evidenceAfterStaleRelease, L11_ID)?.detail_revision,
    }, movedAfterStaleRelease, openCountAfterStale });
  check(report, 'E9(d): the stale A read leaves B pending without adding a member request or showing old root lines',
    frameAfterStaleRelease.revisions?.[L11_ID] === REVISION_B && bStillPending && destinationStillPendingAfterStaleRelease
      && memberCallsAfterStaleRelease.length === callsBeforeStaleRelease
      && rootAfterStaleRelease?.attention.length === 0 && rootAfterStaleRelease.more === null,
    { frameAfterStaleRelease, callsBeforeStaleRelease, memberCallsAfterStaleRelease, bStillPending,
      destinationStillPendingAfterStaleRelease, rootAfterStaleRelease });

  await session.click(`.next-lanes-row-open[data-lane-id=${JSON.stringify(L11_ID)}]`);
  await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level === 'lane'", { timeoutMs: ctx.timeoutMs });
  await session.waitFor("document.querySelectorAll('.next-lanes-scroll .next-lanes-stop-open[data-goal-id]').length === 8", { timeoutMs: ctx.timeoutMs });
  const laneAfterStaleRelease = await laneDetailStops(session);
  const questionCardAfterStaleRelease = await session.eval("document.querySelector('.next-lanes-scroll .next-question-card')?.dataset.questionId || null");
  const expectedBPreviewIds = l11B.inventory.goals.map(goal => goal.goal_id);
  check(report, 'E9(d): stale A draws no stops or question card after the B frame is accepted',
    laneAfterStaleRelease.stops.map(stop => stop.goalId).join('|') === expectedBPreviewIds.join('|')
      && laneAfterStaleRelease.more === '+3 more'
      && questionCardAfterStaleRelease === null
      && destinationStillPendingAfterStaleRelease,
    { laneAfterStaleRelease, expectedBPreviewIds, questionCardAfterStaleRelease,
      destinationStillPendingAfterStaleRelease });
  await session.click('.next-lanes-back');
  await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level === 'all'", { timeoutMs: ctx.timeoutMs });

  const expectedBRootIds = ['Fixture goal 2', 'Fixture goal 1'].map(title => l11B.goals.find(goal => goal.title === title)?.goal_id);

  const bRelease = await releaseObservedPage('the B members release', L11_ID, REVISION_B);
  check(report, 'E9(d): the B members reply is separately released and flushed at revision B without injecting another frame',
    bRelease?.released === true && bRelease.status === 'resolved'
      && bRelease.laneId === L11_ID
      && bRelease.requestedRevision === REVISION_B && bRelease.answeredRevision === REVISION_B
      && bRelease.frameProcessed === true && bRelease.frameSequence === frameBReceipt.frameSequence
      && bRelease.frameSequenceBefore === frameBReceipt.frameSequence
      && bRelease.frameSequenceAfter === frameBReceipt.frameSequence
      && bRelease.renderFrame > staleRelease.renderFrame,
    flushReceipt(bRelease));
  const expectedBRootAttention = ['Fixture goal 2', 'Fixture goal 1'].map(title => {
    const goal = l11B.goals.find(item => item.title === title);
    return { title, reason: blockedReason(goal) };
  });
  await session.waitFor(`(() => {
    const row=document.querySelector('.next-lanes-row-open[data-lane-id=${JSON.stringify(L11_ID)}]')?.closest('.next-lanes-row');
    const lines=[...(row?.querySelectorAll('.next-lanes-attention')||[])].map(line=>({
      title:line.querySelector('.next-lanes-attention-goal')?.textContent||'',
      reason:line.querySelector('.next-lanes-attention-reason')?.textContent||'',
    }));
    const expected = ${JSON.stringify(expectedBRootAttention)};
    return lines.length === expected.length && lines.every((line, index) =>
      line.title === expected[index].title && line.reason === expected[index].reason)
      && row?.querySelector('.next-lanes-more')?.textContent === '+8 more';
  })()`, { timeoutMs: ctx.timeoutMs });
  const rootAfterBRead = await laneRootEvidence(session, L11_ID);
  const actualBRootIds = rootAfterBRead?.attention.map(line => l11B.goals.find(goal => goal.title === line.title)?.goal_id) || [];
  const evidenceAfterBRead = await laneBridgeEvidence(session);
  const bContinuation = evidenceAfterBRead.calls.find(call => call.laneId === L11_ID
    && call.revision === REVISION_B && call.options.goalId == null && Number(call.options.membersLimit) === 32
    && call.options.membersAfter && call.status === 'resolved' && call.answeredRevision === REVISION_B);
  check(report, 'E9(d): the held B members read supplies the exact B root goal IDs after release',
    actualBRootIds.join('|') === expectedBRootIds.join('|')
      && JSON.stringify(rootAfterBRead.attention.map(line => ({ title: line.title, reason: line.reason })))
        === JSON.stringify(expectedBRootAttention)
      && rootAfterBRead.more === '+8 more' && bContinuation
      && evidenceAfterBRead.frameSequence === frameBReceipt.frameSequence
      && normalizedLane(evidenceAfterBRead, L11_ID)?.detail_revision === REVISION_B,
    { rootAfterBRead, expectedBRootIds, expectedBRootAttention, actualBRootIds, frameSequence: evidenceAfterBRead.frameSequence,
      flushSequence: evidenceAfterBRead.renderFrame, bRelease, continuationCursor: bContinuation?.options.membersAfter });

  await session.click(`.next-lanes-row-open[data-lane-id=${JSON.stringify(L11_ID)}]`);
  await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level === 'lane'", { timeoutMs: ctx.timeoutMs });
  await session.waitFor("document.querySelectorAll('.next-lanes-scroll .next-lanes-stop-open[data-goal-id]').length === 11", { timeoutMs: ctx.timeoutMs });
  const laneStopsAtB = await laneDetailStops(session);
  const expectedBIds = l11B.goals.map(goal => goal.goal_id);
  check(report, 'E9(d): after stale A is discarded and B answers, every L11 stop follows B detail order',
    laneStopsAtB.stops.map(stop => stop.goalId).join('|') === expectedBIds.join('|')
      && laneStopsAtB.more === null,
    { laneStopsAtB, expectedBIds });

  const formerGoalB = l11B.goals.find(goal => goal.goal_id === formerGoalId);
  const expectedFormerGoalSpecIds = l11B.members.filter(member => member.goal_id === formerGoalId)
    .map(member => member.spec_id);
  await session.click(`.next-lanes-stop-open[data-goal-id=${JSON.stringify(formerGoalId)}]`);
  await session.waitFor(`window.__nextLanes.pending.some(call => call.laneId === ${JSON.stringify(L11_ID)}
    && call.options.goalId === ${JSON.stringify(formerGoalId)} && call.revision === ${JSON.stringify(REVISION_B)})`,
  { timeoutMs: ctx.timeoutMs });
  const formerGoalRelease = await releaseObservedPage('the former-goal reply', L11_ID, REVISION_B, formerGoalId);
  check(report, 'E9(d): the held former-goal page resolves on the same B frame',
    formerGoalRelease.released && formerGoalRelease.status === 'resolved' && formerGoalRelease.frameProcessed
      && formerGoalRelease.laneId === L11_ID
      && formerGoalRelease.requestedRevision === REVISION_B && formerGoalRelease.answeredRevision === REVISION_B
      && formerGoalRelease.frameSequenceBefore === frameBReceipt.frameSequence
      && formerGoalRelease.frameSequenceAfter === frameBReceipt.frameSequence, flushReceipt(formerGoalRelease));
  await session.waitFor(`(() => {
    return document.querySelector('.next-lanes-scroll')?.dataset.level === 'goal'
      && document.querySelector('.next-lanes-goal-title')?.textContent.trim() === ${JSON.stringify(formerGoalB?.title)}
      && !!document.querySelector('.next-lanes-scroll .next-lanes-section')
      && window.__nextLanes.calls.some(call => call.laneId === ${JSON.stringify(L11_ID)}
        && call.revision === ${JSON.stringify(REVISION_B)}
        && call.options.goalId === ${JSON.stringify(formerGoalId)}
        && call.status === 'resolved' && call.answeredRevision === ${JSON.stringify(REVISION_B)});
  })()`, { timeoutMs: ctx.timeoutMs });
  await revealGoalMemberRows(session);
  await session.waitFor(`(() => {
    const ids=[...document.querySelectorAll('.next-lanes-scroll .next-lanes-member-title[data-spec-id]')]
      .map(row => row.dataset.specId);
    return ${JSON.stringify(expectedFormerGoalSpecIds)}.every(id => ids.includes(id));
  })()`, { timeoutMs: ctx.timeoutMs });
  const formerGoalMemberRows = await visibleGoalMemberRows(session);
  const formerGoalVisibleSpecIds = formerGoalMemberRows.map(row => row.specId);
  const formerGoalReadEvidence = await resolvedGoalReadEvidence(session, L11_ID, REVISION_B, formerGoalId);
  const renderedFormerGoalPairs = formerGoalMemberRows.map(row => `${row.specId}:${row.goalId}`).sort();
  const replyFormerGoalPairs = (formerGoalReadEvidence?.members || []).map(row => `${row.specId}:${row.goalId}`).sort();
  check(report, 'E9(d): the current B former-goal read shows B members and excludes the moved A member row',
    expectedFormerGoalSpecIds.length > 0
      && formerGoalVisibleSpecIds.length === expectedFormerGoalSpecIds.length
      && expectedFormerGoalSpecIds.every(specId => formerGoalVisibleSpecIds.includes(specId))
      && !formerGoalVisibleSpecIds.includes(movedSpecId)
      && formerGoalReadEvidence?.requestedGoalId === formerGoalId
      && formerGoalReadEvidence?.replyRevision === REVISION_B
      && replyFormerGoalPairs.length === expectedFormerGoalSpecIds.length
      && renderedFormerGoalPairs.join('|') === replyFormerGoalPairs.join('|')
      && replyFormerGoalPairs.every(pair => pair.endsWith(`:${formerGoalId}`)),
    { formerGoalId, formerGoalTitle: formerGoalB?.title, expectedFormerGoalSpecIds,
      formerGoalMemberRows, formerGoalReadEvidence, movedSpecId });
  await session.click('.next-lanes-back');
  await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level === 'lane'", { timeoutMs: ctx.timeoutMs });
  await session.click('.next-lanes-back');
  await session.waitFor("document.querySelector('.next-lanes-scroll')?.dataset.level === 'all'", { timeoutMs: ctx.timeoutMs });

  const releasedCurrentLane = await releaseObservedPage('the destination release', movedLaneId, REVISION_B);
  check(report, 'E9(d): the destination lane reply is released and processed at revision B',
    releasedCurrentLane?.released === true && releasedCurrentLane.status === 'resolved'
      && releasedCurrentLane.laneId === movedLaneId
      && releasedCurrentLane.requestedRevision === REVISION_B
      && releasedCurrentLane.answeredRevision === REVISION_B && releasedCurrentLane.frameProcessed === true,
    flushReceipt(releasedCurrentLane));
  await session.waitFor(`!!document.querySelector(${JSON.stringify(`${questionSelector(movedQuestionId)} .next-question-heading-link .next-question-goal-title`)})`, { timeoutMs: ctx.timeoutMs });
  const movedAtB = await questionEvidence(session, movedQuestionId);
  check(report, 'E9(d): after B member evidence lands, the moved question is headed by its new lane and goal',
    movedAtB?.linkTitle?.includes('Fixture moved-member lane')
      && movedAtB.goalTitle === movedLane.goals[0].title && !movedAtB.isChatLink,
    movedAtB);

  await closeQuestionList(session);
  const destinationGoalId = movedLane.goals[0].goal_id;
  await session.click(`.next-lanes-row-open[data-lane-id=${JSON.stringify(movedLaneId)}]`);
  await session.waitFor(`document.querySelector('.next-lanes-scroll')?.dataset.level === 'lane'
    && document.querySelector('.next-lanes-lane-title')?.textContent.trim() === ${JSON.stringify(movedLane.inventory.title)}`,
  { timeoutMs: ctx.timeoutMs });
  await session.click(`.next-lanes-stop-open[data-goal-id=${JSON.stringify(destinationGoalId)}]`);
  await session.waitFor(`(() => {
    return document.querySelector('.next-lanes-scroll')?.dataset.level === 'goal'
      && document.querySelector('.next-lanes-goal-title')?.textContent.trim() === ${JSON.stringify(movedLane.goals[0].title)}
      && !!document.querySelector('.next-lanes-scroll .next-lanes-section')
      && window.__nextLanes.calls.some(call => call.laneId === ${JSON.stringify(movedLaneId)}
        && call.revision === ${JSON.stringify(REVISION_B)}
        && call.options.goalId === ${JSON.stringify(destinationGoalId)}
        && call.status === 'resolved' && call.answeredRevision === ${JSON.stringify(REVISION_B)});
  })()`, { timeoutMs: ctx.timeoutMs });
  await revealGoalMemberRows(session);
  const movedMemberSelector = `.next-lanes-scroll .next-lanes-member-title[data-spec-id=${JSON.stringify(movedSpecId)}]`;
  await session.waitFor(`!!document.querySelector(${JSON.stringify(movedMemberSelector)})`,
    { timeoutMs: ctx.timeoutMs });
  const destinationGoalMemberRows = await visibleGoalMemberRows(session);
  const movedDestinationRows = destinationGoalMemberRows.filter(row => row.specId === movedSpecId);
  const destinationGoalReadEvidence = await resolvedGoalReadEvidence(session, movedLaneId, REVISION_B, destinationGoalId);
  const movedDestinationReplyRows = (destinationGoalReadEvidence?.members || []).filter(row => row.specId === movedSpecId);
  check(report, 'E9(d): the moved A member is visible once in its destination goal under revision B',
    movedDestinationRows.length === 1 && movedDestinationReplyRows.length === 1
      && destinationGoalReadEvidence?.requestedGoalId === destinationGoalId
      && destinationGoalReadEvidence?.replyRevision === REVISION_B
      && movedDestinationRows[0].goalId === movedDestinationReplyRows[0].goalId
      && movedDestinationReplyRows[0].goalId === destinationGoalId
      && !formerGoalVisibleSpecIds.includes(movedSpecId)
      && !l11B.members.some(member => member.spec_id === movedSpecId),
    { formerGoalId, formerGoalVisibleSpecIds, destinationGoalId, destinationGoalMemberRows,
      movedDestinationRows, destinationGoalReadEvidence, movedDestinationReplyRows, movedSpecId, frameRevision: REVISION_B });

  await openQuestionList(ctx);
  await waitForQuestion(ctx, ids[3]);
  const excludedAfterAllReads = await questionEvidence(session, ids[3]);
  const finalFrameEvidence = await currentFrameEvidence(session);
  const finalBridgeEvidence = await laneBridgeEvidence(session);
  const finalQuestionEvidence = await Promise.all(ids.map(id => questionEvidence(session, id)));
  const requiredReadTargets = [
    [PARKED_ID, REVISION_A],
    [L11_ID, REVISION_A],
    [L11_ID, REVISION_B],
    [movedLaneId, REVISION_B],
  ];
  const requiredReadEvidence = requiredReadTargets.map(([laneId, revision]) => {
    const call = finalBridgeEvidence.calls.find(item => item.laneId === laneId && item.revision === revision
      && Number(item.options?.membersLimit) === 32 && item.options?.goalId == null);
    return call ? { laneId, revision, status: call.status, answeredRevision: call.answeredRevision,
      replyRevision: call.reply?.detail_revision || null } : { laneId, revision, missing: true };
  });
  const noUnresolvedReads = finalBridgeEvidence.pending.length === 0
    && finalBridgeEvidence.calls.every(call => call.status === 'resolved')
    && requiredReadEvidence.every(item => item.status === 'resolved'
      && item.answeredRevision === item.revision && item.replyRevision === item.revision);
  check(report, 'E9(b): after every applicable read resolves, the omitted question remains under its own title without chat or Retry',
    excludedAfterAllReads?.ownTitle === 'Synthetic truncated-hidden question'
      && !excludedAfterAllReads.linkTitle && !excludedAfterAllReads.isChatLink && !excludedAfterAllReads.retry
      && finalFrameEvidence.truncated === true
      && !finalFrameEvidence.laneIds.includes(hidden.inventory.lane_id)
      && finalFrameEvidence.counts?.active === 63 && finalFrameEvidence.counts?.paused === 2
      && noUnresolvedReads,
    { excludedAfterAllReads, finalFrameEvidence, pending: finalBridgeEvidence.pending, requiredReadEvidence });
  const finalQuestionPlacements = [
    finalQuestionEvidence[0]?.linkTitle?.includes('Fixture parked lane') && finalQuestionEvidence[0]?.goalTitle === parked.goals[0].title,
    finalQuestionEvidence[1]?.linkTitle?.includes('Fixture ordinary lane') && finalQuestionEvidence[1]?.goalTitle === ordinary.goals[0].title,
    finalQuestionEvidence[2]?.linkTitle?.includes('Fixture late-member lane') && finalQuestionEvidence[2]?.goalTitle === lateB.goals[0].title,
    finalQuestionEvidence[4]?.linkTitle?.includes('Fixture moved-member lane') && finalQuestionEvidence[4]?.goalTitle === movedLane.goals[0].title,
  ];
  check(report, 'E9: all questions with accepted lane evidence are placed while the omitted-lane question stays unresolved',
    finalQuestionPlacements.every(Boolean)
      && finalQuestionEvidence[3]?.ownTitle === 'Synthetic truncated-hidden question'
      && !finalQuestionEvidence[3]?.linkTitle && !finalQuestionEvidence[3]?.isChatLink
      && finalQuestionEvidence.every(item => !item?.retry),
    { finalQuestionEvidence, finalQuestionPlacements, noUnresolvedReads });
  const headingHistory = await session.eval('window.__nextLanesQuestionHeadingGuard?.stop() || []');
  const observedQuestionIds = new Set(headingHistory.filter(item => item.present).map(item => item.questionId));
  const chatLinkedQuestions = headingHistory.filter(item => item.present && item.chatLink);
  const movedOldLaneHeadings = headingHistory.filter(item => item.questionId === movedQuestionId
    && [item.headingText, item.ownTitle, item.linkText].some(value => value?.includes('Fixture lane eleven')));
  check(report, 'E9: the continuous heading guard saw every owned question and recorded no chat link or stale lane-eleven heading',
    ids.every(questionId => observedQuestionIds.has(questionId))
      && chatLinkedQuestions.length === 0 && movedOldLaneHeadings.length === 0,
    { headingHistory, observedQuestionIds: [...observedQuestionIds], chatLinkedQuestions, movedOldLaneHeadings });
}

const nextLaneDataRecovery = isolated(runDataRecovery);
const nextLaneDataEvidence = isolated(runDataEvidence);

module.exports = {
  nextLaneDataRecovery, nextLaneDataEvidence,
  createE9Fixture, startQuestionHeadingObserver, verifyE9Placement, cleanupEvidence,
};
