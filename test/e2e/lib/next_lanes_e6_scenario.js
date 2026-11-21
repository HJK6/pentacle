'use strict';

const { execFileSync } = require('node:child_process');
const crypto = require('node:crypto');
const path = require('node:path');

const F = require('./next_lanes_fixtures');
const H = require('./next_lanes_harness');

const ROOT = path.resolve(__dirname, '../../..');
const SERVICE_DIR = path.join(ROOT, 'services', 'chat-stream-v2');
const COMPOSITE_STREAM = 'local:web-gate-assistant';
const LANE_TITLE = 'Fixture E6 lane';
const GOAL_TITLE = 'Fixture E6 goal';
const AMBIGUOUS_TITLE = 'Fixture shared goal';
const ASSISTANT_TEXT = `${LANE_TITLE} and ${GOAL_TITLE}. ${AMBIGUOUS_TITLE} stays plain.`;

function json(value) { return JSON.stringify(value); }

function check(ctx, label, condition, detail = {}) {
  ctx.report.ok(label, condition === true, detail);
  if (condition !== true) throw new Error(`assertion failed: ${label}`);
}

async function wait(ctx, expression, label) {
  await ctx.session.waitFor(expression, { timeoutMs: ctx.timeoutMs, label });
}

async function press(ctx, key, code, windowsVirtualKeyCode) {
  await ctx.session.send('Input.dispatchKeyEvent', { type: 'keyDown', key, code, windowsVirtualKeyCode });
  await ctx.session.send('Input.dispatchKeyEvent', { type: 'keyUp', key, code, windowsVirtualKeyCode });
}

function seedPendingGoal(python, runtime, epicId, laneId, title) {
  if (typeof python !== 'string' || !python.trim()) {
    throw new Error('E6 seeder requires the selected Python interpreter from the web-gate scenario context');
  }
  const rawScratch = String(runtime?.fixtureScratch || '');
  const port = Number(runtime?.fixtureDaemonPort);
  if (!Number.isSafeInteger(port) || port < 1 || port > 65535 || !path.isAbsolute(rawScratch)) {
    throw new Error('E6 requires the isolated web-gate daemon scratch directory');
  }
  const scratch = path.resolve(rawScratch);
  if (!path.basename(scratch).startsWith('pentacle-web-gate-')) {
    throw new Error('E6 refuses to seed a non-fixture database');
  }
  const dbPath = path.join(scratch, 'sessions.db');
  const script = [
    'import json, sqlite3, sys',
    'sys.path.insert(0, sys.argv[5])',
    'from store_work_index import _state_conn, ensure_work_index_schema',
    'db_path, epic_id, lane_id, title = sys.argv[1:5]',
    'conn = sqlite3.connect(db_path, timeout=10)',
    'try:',
    '    ensure_work_index_schema(conn)',
    '    conn.execute("BEGIN IMMEDIATE")',
    '    state = _state_conn(conn)',
    '    state["available"] = True',
    '    state["root_configured"] = True',
    '    state.setdefault("snapshot_at", "2026-11-21T00:00:00.000Z")',
    '    observations = dict(state.get("goal_observations", {}))',
    '    if epic_id in observations:',
    '        raise RuntimeError("E6 fixture epic already exists")',
    '    observations[epic_id] = {',
    '        "quality": "error", "error": "work_goal_lane_unknown",',
    '        "pending": {',
    '            "facts": {"epic_id": epic_id, "title": title, "lane_id": lane_id, "rank": 1000,',
    '                      "_detail": {"summary_text": "Synthetic goal.", "summary_text_truncated": False,',
    '                                  "summary_line": None, "goal_text": "Synthetic.",',
    '                                  "goal_text_truncated": False}},',
    '            "source_hash": "synthetic:" + epic_id, "path": "epics/" + epic_id + ".md"}}',
    '    state["goal_observations"] = observations',
    '    conn.execute("INSERT INTO v2_work_index_state(singleton,state_json) VALUES (1,?) "',
    '                 "ON CONFLICT(singleton) DO UPDATE SET state_json=excluded.state_json",',
    '                 (json.dumps(state, separators=(",", ":")),))',
    '    conn.commit()',
    'finally:',
    '    conn.close()',
  ].join('\n');
  execFileSync(python,
    ['-c', script, dbPath, epicId, laneId, title, SERVICE_DIR],
    { cwd: SERVICE_DIR, encoding: 'utf8', timeout: 10000 });
}

async function focusPredecessorThenTab(ctx, targetSelector, predecessorSelector, targetDescription) {
  const order = await ctx.session.eval(`(() => {
    const visible = element => {
      const style = getComputedStyle(element);
      return element.tabIndex >= 0 && !element.disabled && style.display !== 'none'
        && style.visibility !== 'hidden' && element.getClientRects().length > 0;
    };
    const controls = [...document.querySelectorAll('a[href],button,input,select,textarea,[tabindex]')].filter(visible);
    const ordered = [...controls.filter(element => element.tabIndex > 0)
      .sort((a,b) => a.tabIndex - b.tabIndex), ...controls.filter(element => element.tabIndex === 0)];
    const target = document.querySelector(${json(targetSelector)});
    const predecessor = document.querySelector(${json(predecessorSelector)});
    const index = ordered.indexOf(target);
    return {
      targetFound: !!target,
      predecessorFound: !!predecessor,
      predecessorIsImmediate: index > 0 && ordered[index - 1] === predecessor,
      predecessorTag: predecessor?.tagName || null,
      predecessorClass: predecessor?.className?.baseVal || predecessor?.className || null,
      predecessorLaneId: predecessor?.dataset?.laneId || null,
      targetIndex: index,
    };
  })()`);
  check(ctx, `${targetDescription}: actual preceding control is in tab order`,
    order.targetFound && order.predecessorFound && order.predecessorIsImmediate, order);

  const focused = await ctx.session.eval(`(() => {
    const previous = document.querySelector(${json(predecessorSelector)});
    previous?.focus();
    return !!previous && document.activeElement === previous;
  })()`);
  check(ctx, `${targetDescription}: focus starts on the preceding control`, focused, order);
  await press(ctx, 'Tab', 'Tab', 9);
  const reached = await ctx.session.eval(`(() => {
    const target = document.querySelector(${json(targetSelector)});
    return !!target && document.activeElement === target;
  })()`);
  check(ctx, `Tab reaches ${targetDescription}`, reached, order);
  await press(ctx, 'Enter', 'Enter', 13);
}

async function closeToAllLanes(ctx) {
  await ctx.session.click('.next-lanes-close');
  await wait(ctx, `document.querySelector('.next-lanes-scroll')?.dataset.level === 'all'`, 'return to All lanes');
}

async function restorePage(ctx, hook, primaryError) {
  const errors = [];
  const attempt = async (label, action) => {
    try { await action(); }
    catch (error) {
      errors.push({ label, name: error.name, message: error.message });
      ctx.report.ok(`E6 cleanup ${label}`, false, errors.at(-1));
    }
  };
  if (hook) await attempt('document hook', () => H.cleanupHook(ctx, hook));
  await attempt('viewport', () => ctx.session.send('Emulation.clearDeviceMetricsOverride'));
  await attempt('page restore', async () => {
    await ctx.session.send('Page.navigate', { url: ctx.url });
    await ctx.session.waitFor("document.readyState === 'complete' && !!document.getElementById('session-list')",
      { timeoutMs: ctx.timeoutMs });
  });
  ctx.report.ok('E6 cleanup completed', errors.length === 0, { errors });
  if (errors.length && primaryError) primaryError.cleanupErrors = errors;
  if (errors.length && !primaryError) throw new AggregateError(errors.map(row => new Error(row.message)), 'E6 cleanup failed');
}

async function nextLaneLinks(ctx) {
  let hook = null;
  let primaryError = null;
  try {
    if (!ctx.fixture || !ctx.runtime?.fixtureDaemonPort || !ctx.runtime?.fixtureScratch) {
      throw new Error('next-lane-links requires the isolated web-gate fixture daemon');
    }

    const nonce = crypto.randomBytes(6).toString('hex');
    const laneKey = `e6-${nonce}`;
    const adoptionKey = `spec:next_lanes_fixture_${laneKey}`;
    const laneId = F.fixtureLaneId(laneKey);
    const epicId = `epic_fixture_next_lanes_e6_${nonce}`;
    const goalId = F.goalId(epicId);
    const proseRequestId = `next-lanes-e6-prose-${nonce}`;
    const adoptRequestId = `next-lanes-e6-adopt-${nonce}`;

    const linkedLane = F.makeOrdinaryLane({
      laneId,
      title: LANE_TITLE,
      goalTitle: GOAL_TITLE,
      goalId,
      memberCount: 1,
      state: 'paused',
    });
    const ambiguousA = F.makeOrdinaryLane({
      laneId: F.ORDINARY_A_ID,
      title: 'Fixture link lane A',
      goalTitle: AMBIGUOUS_TITLE,
      goalId: F.goalId('epic_fixture_next_lanes_e6_shared_a'),
      memberCount: 1,
    });
    const ambiguousB = F.makeOrdinaryLane({
      laneId: F.ORDINARY_B_ID,
      title: 'Fixture link lane B',
      goalTitle: AMBIGUOUS_TITLE,
      goalId: F.goalId('epic_fixture_next_lanes_e6_shared_b'),
      memberCount: 1,
    });
    const lanes = [linkedLane, ambiguousA, ambiguousB];
    const inventory = F.inventoryFrame(lanes);
    const laneSelector = `.next-lane-name-link[data-lane-id="${laneId}"]:not([data-goal-id])`;
    const goalSelector = `.next-lane-name-link[data-lane-id="${laneId}"][data-goal-id="${goalId}"]`;
    const updateSelector = `.slot-chat-lane-update[data-lane-id="${laneId}"]`;

    seedPendingGoal(ctx.python, ctx.runtime, epicId, laneId, GOAL_TITLE);
    hook = await H.enterNext(ctx, 1440);
    await H.installLaneBridge(ctx, { frame: inventory, versions: H.laneDetailVersions(lanes) });
    await wait(ctx, `!!document.querySelector('.next-lanes-scroll')`, 'E6 lanes card mounted');

    const adopt = await H.fixtureRequestWithPublication(ctx.runtime, H.UNLISTED_PRODUCER, {
      type: 'assistant.operation',
      request_id: adoptRequestId,
      composite_stream_id: COMPOSITE_STREAM,
      dispatch_id: 'none',
      operation: 'work_lane.adopt',
      payload: {
        adoption_key: adoptionKey,
        title: LANE_TITLE,
        summary: GOAL_TITLE,
        owner_kind: 'operator',
        work_state: 'paused',
        visible_chat: { stream_id: COMPOSITE_STREAM },
        goals: [epicId],
        no_spec_reason: 'Synthetic E6 fixture lane.',
        emit_started: true,
      },
    }, event => event?.stream_id === COMPOSITE_STREAM && event?.publish_kind === 'lane_update'
      && event?.raw?.lane_update?.lane_id === laneId && event?.raw?.lane_update?.source?.id === adoptRequestId);
    const laneUpdate = adopt.publication;
    const laneUpdateData = laneUpdate?.raw?.lane_update;
    check(ctx, 'daemon adopts the exact fixture lane and publishes a stored lane update',
      adopt.reply?.type === 'assistant.operation.ok' && adopt.reply?.lane?.lane_id === laneId
        && laneUpdate?.stream_id === COMPOSITE_STREAM && laneUpdate?.publish_kind === 'lane_update'
        && laneUpdateData?.lane_id === laneId && laneUpdateData?.kind === 'lane_started'
        && laneUpdateData?.source?.type === 'adoption' && laneUpdateData?.owner_kind === 'operator'
        && laneUpdateData?.update_id === laneUpdate?.message_id?.replace(/^publication:/, '')
        && laneUpdate?.raw?.actor_stream_id === H.UNLISTED_PRODUCER
        && laneUpdate?.raw?.assistant_composite?.published_by?.stream_id === H.UNLISTED_PRODUCER
        && !!laneUpdate?.raw?.assistant_composite?.published_by?.generation,
      {
        laneId,
        messageId: laneUpdate?.message_id || null,
        updateId: laneUpdateData?.update_id || null,
        source: laneUpdateData?.source || null,
        ownerKind: laneUpdateData?.owner_kind || null,
        publisher: laneUpdate?.raw?.assistant_composite?.published_by || null,
      });
    await wait(ctx, `!!document.querySelector(${json(updateSelector)})`, 'stored lane update appears in assistant transcript');

    const prose = await H.fixtureRequestWithPublication(ctx.runtime, H.UNLISTED_PRODUCER, {
      type: 'assistant.publish',
      request_id: proseRequestId,
      composite_stream_id: COMPOSITE_STREAM,
      publish_kind: 'prose',
      message: ASSISTANT_TEXT,
      note_type: 'started',
      ref: `lane:${laneId}`,
    }, event => event?.stream_id === COMPOSITE_STREAM && event?.publish_kind === 'prose'
      && event?.text === ASSISTANT_TEXT && event?.message_id === `publication:${proseRequestId}`);
    check(ctx, 'assistant link text is a production stored prose publication',
      prose.reply?.type === 'assistant.publish.ok' && prose.publication?.publish_kind === 'prose'
        && prose.publication?.message_id === `publication:${proseRequestId}`
        && prose.publication?.raw?.published_by?.stream_id === H.UNLISTED_PRODUCER
        && !!prose.publication?.raw?.published_by?.generation,
      {
        messageId: prose.publication?.message_id || null,
        publisher: prose.publication?.raw?.published_by || null,
      });
    await wait(ctx, `([...document.querySelectorAll('.slot-chat-assistant-card')]).some(card => card.textContent.includes(${json(ASSISTANT_TEXT)}))`,
      'stored prose appears with lane name targets');

    const evidence = await ctx.session.eval(`window.__nextLanesGetEvidence?.() || null`);
    const targetEvidence = await ctx.session.eval(`(() => {
      const inventory = window.__nextLanesGetEvidence?.()?.normalizedInventory;
      const lanes = inventory?.lanes || [];
      const targets = lanes.flatMap(lane => [
        { title: lane.title, laneId: lane.lane_id, goalId: null },
        ...(lane.goals || []).map(goal => ({ title: goal.title, laneId: lane.lane_id, goalId: goal.goal_id })),
      ]);
      return {
        sourceOwned: window.__nextLanesGetEvidence?.()?.sourceOwned === true,
        laneIds: lanes.map(lane => lane.lane_id),
        linkedLane: targets.some(target => target.title === ${json(LANE_TITLE)}
          && target.laneId === ${json(laneId)} && target.goalId === null),
        linkedGoal: targets.some(target => target.title === ${json(GOAL_TITLE)}
          && target.laneId === ${json(laneId)} && target.goalId === ${json(goalId)}),
        ambiguous: targets.filter(target => target.title === ${json(AMBIGUOUS_TITLE)} && target.goalId)
          .map(target => ({ laneId: target.laneId, goalId: target.goalId })),
      };
    })()`);
    const ambiguousTargets = targetEvidence.ambiguous || [];
    check(ctx, 'normalized lane name index contains both exact ambiguous goal targets',
      targetEvidence.sourceOwned && targetEvidence.laneIds.includes(laneId)
        && targetEvidence.linkedLane && targetEvidence.linkedGoal
        && ambiguousTargets.length === 2
        && ambiguousTargets.some(target => target.laneId === ambiguousA.inventory.lane_id
          && target.goalId === ambiguousA.goals[0].goal_id)
        && ambiguousTargets.some(target => target.laneId === ambiguousB.inventory.lane_id
          && target.goalId === ambiguousB.goals[0].goal_id),
      { laneId, goalId, ambiguousTargets, acceptedFrames: evidence?.acceptedFrames?.length || 0 });

    const rendered = await ctx.session.eval(`(() => {
      const card = [...document.querySelectorAll('.slot-chat-assistant-card')]
        .find(row => row.textContent.includes(${json(ASSISTANT_TEXT)}));
      const links = [...(card?.querySelectorAll('.next-lane-name-link') || [])]
        .map(link => ({ text: link.textContent.trim(), laneId: link.dataset.laneId, goalId: link.dataset.goalId || '' }));
      return {
        prose: card?.textContent || '',
        links,
        ambiguousLinks: links.filter(link => link.text === ${json(AMBIGUOUS_TITLE)}).length,
        exactLaneLink: links.some(link => link.text === ${json(LANE_TITLE)} && link.laneId === ${json(laneId)} && !link.goalId),
        exactGoalLink: links.some(link => link.text === ${json(GOAL_TITLE)} && link.laneId === ${json(laneId)} && link.goalId === ${json(goalId)}),
        updateCard: (() => { const row = document.querySelector(${json(updateSelector)}); return row ? {
          laneId: row.dataset.laneId, updateId: row.dataset.updateId, kind: row.dataset.updateKind,
        } : null; })(),
      };
    })()`);
    check(ctx, 'unique names link to exact lane and goal while shared title stays plain',
      rendered.exactLaneLink && rendered.exactGoalLink && rendered.ambiguousLinks === 0
        && rendered.prose.includes(`${AMBIGUOUS_TITLE} stays plain.`)
        && rendered.updateCard?.laneId === laneId && rendered.updateCard?.updateId === laneUpdateData.update_id,
      rendered);

    const history = await ctx.session.eval(`(async () => {
      const reply = await window.cc.requestStreamEvents({ streamId: ${json(COMPOSITE_STREAM)}, limit: 100 });
      if (!reply?.ok) return { ok: false, error: reply?.error || null, ids: [] };
      const events = window.PentacleChatStore.getState().events.filter(event => event.stream_id === ${json(COMPOSITE_STREAM)});
      return { ok: true, ids: events.map(event => event.message_id), received: reply.received ?? null };
    })()`);
    check(ctx, 'both captured publications are present in daemon history readback',
      history?.ok === true && history.ids.includes(laneUpdate.message_id)
        && history.ids.includes(prose.publication.message_id),
      { ok: history?.ok, laneUpdateId: laneUpdate.message_id, proseId: prose.publication.message_id,
        present: history?.ids?.filter(id => [laneUpdate.message_id, prose.publication.message_id].includes(id)) || [] });

    await ctx.session.click(laneSelector);
    await wait(ctx, `document.querySelector('.next-lanes-scroll')?.dataset.level === 'lane'`, 'click lane name opens lane');
    check(ctx, 'clicking the lane name opens the exact lane',
      await ctx.session.eval(`document.querySelector('.next-lanes-lane-title')?.textContent === ${json(LANE_TITLE)}`));
    await closeToAllLanes(ctx);

    await ctx.session.click(goalSelector);
    await wait(ctx, `document.querySelector('.next-lanes-scroll')?.dataset.level === 'goal'`, 'click goal name opens goal');
    check(ctx, 'clicking the unique goal name opens the exact goal',
      await ctx.session.eval(`document.querySelector('.next-lanes-goal-title')?.textContent === ${json(GOAL_TITLE)}`));
    await closeToAllLanes(ctx);

    await focusPredecessorThenTab(ctx, laneSelector, updateSelector, 'lane name');
    await wait(ctx, `document.querySelector('.next-lanes-scroll')?.dataset.level === 'lane'`, 'Tab then Enter opens lane');
    check(ctx, 'Tab then Enter on the lane name opens its lane',
      await ctx.session.eval(`document.querySelector('.next-lanes-lane-title')?.textContent === ${json(LANE_TITLE)}`));
    await closeToAllLanes(ctx);

    await focusPredecessorThenTab(ctx, goalSelector, laneSelector, 'unique goal name');
    await wait(ctx, `document.querySelector('.next-lanes-scroll')?.dataset.level === 'goal'`, 'Tab then Enter opens goal');
    check(ctx, 'Tab then Enter on the unique goal name opens that goal',
      await ctx.session.eval(`document.querySelector('.next-lanes-goal-title')?.textContent === ${json(GOAL_TITLE)}`));
    await closeToAllLanes(ctx);

    await ctx.session.click(`${updateSelector}[data-update-id="${laneUpdateData.update_id}"]`);
    await wait(ctx, `document.querySelector('.next-lanes-scroll')?.dataset.level === 'lane'`, 'exact update card opens lane');
    check(ctx, 'the exact stored lane-update card opens its own lane',
      await ctx.session.eval(`document.querySelector('.next-lanes-lane-title')?.textContent === ${json(LANE_TITLE)}`),
      { laneId, updateId: laneUpdateData.update_id });
  } catch (error) {
    primaryError = error;
    throw error;
  } finally {
    await restorePage(ctx, hook, primaryError);
  }
}

module.exports = { nextLaneLinks };
