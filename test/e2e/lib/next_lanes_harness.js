'use strict';

const crypto = require('node:crypto');
const WebSocket = require('ws');
const { fixtureRequest } = require('./closed_chat_scenario');
const { pageLaneDetail, questionFixture } = require('./next_lanes_fixtures');

const PRODUCER = 'local:web-gate-1';
const UNLISTED_PRODUCER = 'local:web-gate-voice-producer';
const QUESTION_PREFIX = 'next_lanes__';
const QUESTION_OWNERS = new WeakMap();

function json(value) { return JSON.stringify(value); }

function hookSource(documentMarker) {
  return `(() => {
    const NativeWebSocket = window.WebSocket;
    window.__nextLanesSocketSequence = 0;
    window.__nextLanesSocket = null;
    if (NativeWebSocket && !window.__nextLanesWebSocketWrapped) {
      window.WebSocket = new Proxy(NativeWebSocket, {
        construct(target, args, newTarget) {
          const socket = Reflect.construct(target, args, newTarget);
          try {
            const endpoint = new URL(String(args[0]), window.location.href);
            if (endpoint.pathname.replace(/\\/+$/, '') === '/cc') {
              window.__nextLanesSocket = socket;
              window.__nextLanesSocketSequence += 1;
            }
          } catch (_) { /* Leave unrelated socket construction untouched. */ }
          return socket;
        },
      });
      window.__nextLanesWebSocketWrapped = true;
    }
    const documentMarker = ${json(documentMarker)};
    window.__nextLanesDocumentMarker = documentMarker;
    window.__nextLanesSourceOwned = true;
    window.__nextLanesHeldDaemonFrames = [];
    window.__nextLanesSourceEvents = [];
    window.__nextLanesSubscriptionSequence = 0;
    window.__nextLanesSubscriptionEvents = [];
    window.__nextLanesRendererCallback = null;
    window.__nextLanesBaseRendererCallback = null;
    window.__nextLanesPromptCalls = [];
    window.__nextLanesPromptReads = [];
    window.__nextLanesNotificationCalls = [];
    window.__nextLanesGetEvidence = () => ({
      sourceOwned: window.__nextLanesSourceOwned,
      frameSequence: window.__nextLanes?.frameSequence || 0,
      renderFrame: window.__nextLanes?.renderFrame || 0,
      rawFrame: window.__nextLanes?.currentFrame || null,
      normalizedInventory: window.__nextLanes?.currentNormalizedInventory
        || window.PentacleChatCore?.emptyWorkLanesInventory?.() || null,
      acceptedFrames: window.__nextLanes?.acceptedFrames || [],
      sourceEvents: window.__nextLanesSourceEvents,
      heldDaemonFrames: window.__nextLanesHeldDaemonFrames,
      socketSequence: window.__nextLanesSocketSequence || 0,
      subscriptionSequence: window.__nextLanesSubscriptionSequence || 0,
      subscriptionEvents: window.__nextLanesSubscriptionEvents,
    });
    function recordSource(source, frame, action) {
      const row = frame && typeof frame === 'object' ? frame : {};
      const hasWorkLanes = row.type === 'work_lanes.inventory' || Object.prototype.hasOwnProperty.call(row, 'work_lanes');
      window.__nextLanesSourceEvents.push({
        source,
        type: row.type || (source === 'getChatStreamState' ? 'snapshot' : null),
        state_version: row.state_version ?? null,
        hadWorkLanes: hasWorkLanes,
        open: row.work_lanes?.counts?.open ?? row.counts?.open ?? null,
        action,
        connectedPresent: Object.prototype.hasOwnProperty.call(row, 'connected'),
        connected: typeof row.connected === 'boolean' ? row.connected : null,
        sessionsPresent: Array.isArray(row.sessions),
        notificationsPresent: Object.prototype.hasOwnProperty.call(row, 'notifications'),
      });
    }
    let cc;
    function wrap(value) {
      if (!value || value.__nextLanesHooked) return value;
      Object.defineProperty(value, '__nextLanesHooked', { value: true, configurable: true });
      if (typeof value.onChatStreamFrame === 'function') {
        const subscribe = value.onChatStreamFrame;
        value.onChatStreamFrame = function(callback) {
          const previousCallback = window.__nextLanesRendererCallback;
          window.__nextLanesRendererCallback = callback;
          if (!window.__nextLanesBaseRendererCallback) window.__nextLanesBaseRendererCallback = callback;
          const subscriptionSequence = ++window.__nextLanesSubscriptionSequence;
          window.__nextLanesSubscriptionEvents.push({
            sequence: subscriptionSequence,
            callbackReplaced: previousCallback !== null && previousCallback !== callback,
            currentFrameAvailable: !!window.__nextLanes?.currentFrame,
          });
          // The scenario feeds its owned inventory through the same renderer
          // callback as daemon frames. Reassign on each subscription so a
          // reconnect cannot leave the bridge pointed at a stale listener.
          window.__nextLanesFrameSink = callback;
          const sourceSink = frame => {
            if (window.__nextLanesSourceOwned && frame && frame.type === 'work_lanes.inventory') {
              window.__nextLanesHeldDaemonFrames.push({ type: frame.type, state_version: frame.state_version ?? null });
              recordSource('onChatStreamFrame', frame, 'withheld');
              return undefined;
            }
            if (window.__nextLanesSourceOwned && frame && Object.prototype.hasOwnProperty.call(frame, 'work_lanes')) {
              window.__nextLanesHeldDaemonFrames.push({ type: frame.type || null, state_version: frame.state_version ?? null,
                open: frame.work_lanes?.counts?.open ?? null });
              const withoutLaneInventory = { ...frame };
              delete withoutLaneInventory.work_lanes;
              recordSource('onChatStreamFrame', frame, 'stripped');
              return callback(withoutLaneInventory);
            }
            return callback(frame);
          };
          window.__nextLanesSourceFrameSink = sourceSink;
          if (window.__nextLanesSourceOwned && window.__nextLanes?.currentFrame) {
            queueMicrotask(() => callback(window.__nextLanes.currentFrame));
          }
          return subscribe.call(this, sourceSink);
        };
      }
      if (typeof value.getChatStreamState === 'function') {
        const getState = value.getChatStreamState;
        value.getChatStreamState = function(...args) {
          return Promise.resolve(getState.apply(this, args)).then(state => {
            if (!state || typeof state !== 'object') return state;
            const hasWorkLanes = Object.prototype.hasOwnProperty.call(state, 'work_lanes');
            if (window.__nextLanesSourceOwned && hasWorkLanes) {
              const withoutLaneInventory = { ...state };
              delete withoutLaneInventory.work_lanes;
              recordSource('getChatStreamState', state, 'stripped');
              return withoutLaneInventory;
            }
            recordSource('getChatStreamState', state, 'passed');
            return state;
          });
        };
      }
      if (typeof value.promptList === 'function') {
        const promptList = value.promptList;
        value.promptList = function(args) {
          const callArgs = args && typeof args === 'object' ? { ...args } : args ?? null;
          const call = { sequence: window.__nextLanesPromptReads.length + 1, args: callArgs, status: 'pending', questionIds: [], records: [] };
          window.__nextLanesPromptCalls.push(callArgs);
          window.__nextLanesPromptReads.push(call);
          if (window.__nextQuestionResolveGate) window.__nextQuestionResolveGate.timeline.push({ type: 'prompt.list:start', sequence: call.sequence, args: callArgs, at: performance.now() });
          let result;
          try { result = promptList.call(this, args); }
          catch (error) { call.status = 'failed'; call.error = String(error?.message || error); throw error; }
          return Promise.resolve(result).then(reply => {
            call.status = reply?.ok === false ? 'failed' : 'resolved';
            const rows = Array.isArray(reply?.questions) ? reply.questions : [];
            call.questionIds = rows.map(row => row?.question_id).filter(Boolean);
            call.records = rows.map(row => ({ question_id: row?.question_id || null, state: row?.state || null,
              hasStoredAnswer: Boolean(row?.answer && (row.answer.selections?.length || row.answer.custom_text || row.answer.answer != null)),
              selections: Array.isArray(row?.answer?.selections) ? [...row.answer.selections] : [],
              customText: typeof row?.answer?.custom_text === 'string' ? row.answer.custom_text : null }));
            if (window.__nextQuestionResolveGate) window.__nextQuestionResolveGate.timeline.push({ type: 'prompt.list:resolved', sequence: call.sequence, args: callArgs,
              questionIds: [...call.questionIds], at: performance.now() });
            return reply;
          }, error => {
            call.status = 'failed'; call.error = String(error?.message || error);
            if (window.__nextQuestionResolveGate) window.__nextQuestionResolveGate.timeline.push({ type: 'prompt.list:failed', sequence: call.sequence, args: callArgs, at: performance.now() });
            throw error;
          });
        };
      }
      if (typeof value.notificationList === 'function') {
        const notificationList = value.notificationList;
        value.notificationList = function(args) {
          window.__nextLanesNotificationCalls.push(args && typeof args === 'object' ? { ...args } : args ?? null);
          return notificationList.call(this, args);
        };
      }
      return value;
    }
    window.__nextLanesEmitSourceFrame = frame => {
      if (typeof window.__nextLanesSourceFrameSink !== 'function') throw new Error('wrapped source frame sink unavailable');
      return window.__nextLanesSourceFrameSink(frame);
    };
    Object.defineProperty(window, 'cc', {
      configurable: true,
      enumerable: true,
      get: () => cc,
      set: value => { cc = wrap(value); },
    });
    if (window.cc) cc = wrap(window.cc);
  })();`;
}

async function installFrameHook(ctx) {
  const documentMarker = crypto.randomUUID();
  const installed = await ctx.session.send('Page.addScriptToEvaluateOnNewDocument', {
    source: hookSource(documentMarker),
  });
  return { ...installed, documentMarker };
}

async function enterNext(ctx, width = 1440) {
  const hook = await installFrameHook(ctx);
  try {
    await ctx.session.send('Emulation.setDeviceMetricsOverride', {
      width, height: width < 600 ? 956 : 900, deviceScaleFactor: 1, mobile: false,
    });
    await ctx.session.send('Page.navigate', { url: `${ctx.url}next/` });
    await ctx.session.waitFor(`window.__nextLanesDocumentMarker === ${json(hook.documentMarker)} && !!window.cc && !!window.PentacleChatStore
      && document.querySelectorAll('.rail-button').length === 6`, { timeoutMs: ctx.timeoutMs });
    await ctx.session.eval("document.documentElement.dataset.theme='dark'; localStorage.setItem('pentacle.next.theme','dark'); true");
    await ctx.session.waitFor("window.cc.getChatStreamState().then(s => s.connected === true && (s.sessions || []).length > 0)", { timeoutMs: ctx.timeoutMs });
    await ctx.session.waitFor('typeof window.__nextLanesFrameSink === "function"', { timeoutMs: ctx.timeoutMs });
    return hook;
  } catch (error) {
    try { await cleanupHook(ctx, hook); }
    catch (cleanupError) {
      error.cleanupError = {
        name: cleanupError.name || 'Error',
        message: String(cleanupError.message || cleanupError),
        ...(cleanupError.stack ? { stack: String(cleanupError.stack) } : {}),
      };
    }
    try { await ctx.session.send('Emulation.clearDeviceMetricsOverride'); } catch { /* best effort */ }
    throw error;
  }
}

async function reconnectAndReplaceSubscription(ctx) {
  const { session, timeoutMs } = ctx;
  const before = await session.eval(`(() => ({
    socketSequence: window.__nextLanesSocketSequence || 0,
    socketReadyState: window.__nextLanesSocket?.readyState ?? null,
    snapshotReads: (window.__nextLanesSourceEvents || []).filter(row => row.source === 'getChatStreamState').length,
    subscriptionSequence: window.__nextLanesSubscriptionSequence || 0,
    frameSequence: window.__nextLanes?.frameSequence ?? null,
  }))()`);
  if (before.socketReadyState !== 1) throw new Error('captured /cc socket is not open before reconnect');
  await session.eval(`(() => {
    if (!window.__nextLanesSocket) throw new Error('captured /cc socket is unavailable');
    window.__nextLanesSocket.close();
    return true;
  })()`);
  await session.waitFor(`window.__nextLanesSocketSequence > ${before.socketSequence}
    && window.__nextLanesSocket?.readyState === 1
    && window.cc.webConnectionState?.()?.open === true`,
  { timeoutMs, label: 'real /cc WebSocket reconnects and reopens' });
  await session.waitFor(`(window.__nextLanesSourceEvents || []).filter(row => row.source === 'getChatStreamState').length > ${before.snapshotReads}`,
    { timeoutMs, label: 'renderer reads its direct connection snapshot after reconnect' });

  const readback = await session.eval(`(async () => {
    const snapshot = await window.cc.getChatStreamState();
    const store = window.PentacleChatStore?.getState?.() || {};
    const questionId = row => row?.question?.question_id || row?.question_id || null;
    return {
      transportOpen: window.cc.webConnectionState?.()?.open === true,
      snapshotConnected: snapshot?.connected === true,
      snapshotHasWorkLanes: Object.prototype.hasOwnProperty.call(snapshot || {}, 'work_lanes'),
      snapshotSessionIds: (snapshot?.sessions || []).map(row => row?.stream_id).filter(Boolean),
      snapshotNotificationQuestionIds: (snapshot?.notifications || []).map(questionId).filter(Boolean),
      storeSessionIds: (store.sessions || []).map(row => row?.stream_id).filter(Boolean),
      storeNotificationQuestionIds: (store.notifications || []).map(questionId).filter(Boolean),
      reconnectSnapshotEvents: (window.__nextLanesSourceEvents || [])
        .filter(row => row.source === 'getChatStreamState').slice(${before.snapshotReads}),
    };
  })()`);
  const replacement = await session.eval('window.__nextLanesRebindRendererAndFlush()');
  const after = await session.eval(`(() => ({
    socketSequence: window.__nextLanesSocketSequence || 0,
    socketReadyState: window.__nextLanesSocket?.readyState ?? null,
    subscriptionSequence: window.__nextLanesSubscriptionSequence || 0,
    frameSequence: window.__nextLanes?.frameSequence ?? null,
    renderFrame: window.__nextLanes?.renderFrame ?? null,
    currentRevision: window.__nextLanes?.currentFrame?.lanes?.map(row => [row.lane_id, row.detail_revision]) || [],
  }))()`);
  return { before, readback, replacement, after };
}

function laneDetailVersions(lanes) {
  const byLane = {};
  for (const lane of lanes || []) {
    const revision = lane.detailRevision || lane.inventory?.detail_revision;
    if (!lane.inventory?.lane_id || !revision) continue;
    byLane[lane.inventory.lane_id] = { ...(byLane[lane.inventory.lane_id] || {}), [revision]: lane };
  }
  return byLane;
}

async function installLaneBridge(ctx, { frame = null, versions = {}, failures = [], holdRules = [] } = {}) {
  await ctx.session.waitFor('!!window.cc && typeof window.__nextLanesFrameSink === "function"', { timeoutMs: ctx.timeoutMs });
  await ctx.session.eval(`(async () => {
    const control = {
      currentFrame: null,
      versions: ${json(versions)},
      failures: new Set(${json(failures)}),
      holdRules: ${json(holdRules)}.map(rule => ({ ...rule })),
      calls: [],
      pending: [],
      frameSequence: 0,
      renderFrame: 0,
      currentNormalizedInventory: null,
      acceptedFrames: [],
      releaseReceipts: [],
      subscriptionReceipts: [],
      captureRefusals: [],
    };
    const pageLaneDetail = ${pageLaneDetail.toString()};
    function normalizedFrame(frame) {
      const core = window.PentacleChatCore;
      if (!core || typeof core.normalizeWorkLanesInventory !== 'function') throw new Error('production work-lanes normalizer unavailable');
      if (frame.lanes.some(row => Object.prototype.hasOwnProperty.call(row, 'progress'))) {
        throw new Error('raw inventory contains normalized nested progress');
      }
      const inventory = core.normalizeWorkLanesInventory(frame);
      if (!inventory || inventory.lanes.length !== frame.lanes.length) throw new Error('raw inventory was not accepted by the production normalizer');
      for (const raw of frame.lanes) {
        const lane = inventory.lanes.find(item => item.lane_id === raw.lane_id);
        if (!lane?.progress || !Array.isArray(raw.members) || lane.progress.members.length !== raw.members.length
          || lane.progress.members_total !== raw.members_total) throw new Error('normalized preview mismatch for ' + raw.lane_id);
        for (let index = 0; index < raw.members.length; index += 1) {
          const before = raw.members[index]; const after = lane.progress.members[index];
          if (before.spec_id !== after.spec_id || (before.goal_id ?? null) !== (after.goal_id ?? null)
            || Number(before.open_questions?.count || 0) !== after.open_questions.count
            || (before.open_questions?.first_question_id ?? null) !== (after.open_questions.first_question_id ?? null)) {
            throw new Error('normalized member evidence mismatch for ' + raw.lane_id + '/' + before.spec_id);
          }
        }
      }
      return inventory;
    }
    window.__nextLanesGetEvidence = () => ({
      sourceOwned: window.__nextLanesSourceOwned,
      frameSequence: control.frameSequence,
      renderFrame: control.renderFrame,
      rawFrame: control.currentFrame ? JSON.parse(JSON.stringify(control.currentFrame)) : null,
      normalizedInventory: control.currentNormalizedInventory
        ? JSON.parse(JSON.stringify(control.currentNormalizedInventory))
        : window.PentacleChatCore?.emptyWorkLanesInventory?.() || null,
      acceptedFrames: JSON.parse(JSON.stringify(control.acceptedFrames)),
      sourceEvents: JSON.parse(JSON.stringify(window.__nextLanesSourceEvents || [])),
      heldDaemonFrames: JSON.parse(JSON.stringify(window.__nextLanesHeldDaemonFrames || [])),
      socketSequence: window.__nextLanesSocketSequence || 0,
      subscriptionSequence: window.__nextLanesSubscriptionSequence || 0,
      subscriptionEvents: JSON.parse(JSON.stringify(window.__nextLanesSubscriptionEvents || [])),
      calls: JSON.parse(JSON.stringify(control.calls)),
      pending: control.pending.map(item => ({ laneId: item.laneId, revision: item.revision, options: { ...item.options } })),
      releaseReceipts: JSON.parse(JSON.stringify(control.releaseReceipts)),
      subscriptionReceipts: JSON.parse(JSON.stringify(control.subscriptionReceipts)),
    });
    function currentRevision(laneId) {
      return control.currentFrame?.lanes?.find(row => row.lane_id === laneId)?.detail_revision || null;
    }
    function laneVersion(laneId, requestedRevision) {
      const rows = control.versions[laneId] || {};
      const lane = rows[requestedRevision];
      if (!lane) throw new Error('synthetic lane detail unavailable for requested revision');
      const actual = lane.detailRevision || lane.inventory?.detail_revision;
      if (actual !== requestedRevision) throw new Error('synthetic lane detail revision mismatch');
      return lane;
    }
    function replyFor(laneId, options = {}, requestedRevision = currentRevision(laneId)) {
      const lane = laneVersion(laneId, requestedRevision);
      return pageLaneDetail(lane, options);
    }
    function findPending(match) {
      return control.pending.findIndex(item => (!match?.laneId || item.laneId === match.laneId)
        && (match?.membersLimit == null || Number(item.options.membersLimit) === match.membersLimit)
        && (!match?.goalId || (item.options.goalId || null) === match.goalId)
        && (!match?.revision || item.revision === match.revision));
    }
    function takePending(index) {
      if (index < 0) return null;
      const [pending] = control.pending.splice(index, 1);
      pending.call.status = 'resolved';
      pending.call.answeredRevision = pending.reply?.detail_revision || null;
      pending.resolve(pending.reply);
      return pending;
    }
    async function twoFrameFlush() {
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      control.renderFrame += 1;
      return control.renderFrame;
    }
    window.__nextLanes = control;
    window.__nextLanesSourceOwned = true;
    window.__nextLanesSetInventory = async next => {
      if (!next || !Array.isArray(next.lanes)) throw new Error('synthetic inventory frame needs a lanes array');
      const { state_version: _sourceStateVersion, ...rawFrame } = next;
      const frameToEmit = JSON.parse(JSON.stringify(rawFrame));
      const normalized = normalizedFrame(frameToEmit);
      control.currentFrame = frameToEmit;
      control.currentNormalizedInventory = normalized;
      control.frameSequence += 1;
      const sink = window.__nextLanesFrameSink;
      if (typeof sink !== 'function') throw new Error('renderer lane frame sink unavailable');
      sink(frameToEmit);
      const renderFrame = await twoFrameFlush();
      const accepted = {
        frameSequence: control.frameSequence,
        renderFrame,
        rawFrame: JSON.parse(JSON.stringify(frameToEmit)),
        normalizedInventory: JSON.parse(JSON.stringify(normalized)),
      };
      control.acceptedFrames.push(accepted);
      return { accepted: true, frameProcessed: true, frameSequence: control.frameSequence,
        stateVersion: null, renderFrame, normalizedInventory: accepted.normalizedInventory };
    };
    window.__nextLanesRelease = index => {
      takePending(index || 0);
      return control.pending.length;
    };
    window.__nextLanesReleaseMatching = match => {
      const pending = takePending(findPending(match || {}));
      return !!pending;
    };
    window.__nextLanesReleaseMatchingAndFlush = async match => {
      const frameSequenceBefore = control.frameSequence;
      const pending = takePending(findPending(match || {}));
      if (!pending) return { released: false, frameProcessed: false, laneId: match?.laneId || null,
        requestedRevision: match?.revision || null, answeredRevision: null, renderFrame: control.renderFrame,
        frameSequenceBefore, frameSequenceAfter: control.frameSequence, frameSequence: control.frameSequence };
      const renderFrame = await twoFrameFlush();
      const receipt = {
        released: true,
        callId: pending.call.callId,
        laneId: pending.laneId,
        requestedRevision: pending.revision,
        answeredRevision: pending.reply?.detail_revision || null,
        status: pending.call.status,
        frameProcessed: true,
        renderFrame,
        frameSequence: control.frameSequence,
        frameSequenceBefore,
        frameSequenceAfter: control.frameSequence,
      };
      control.releaseReceipts.push(receipt);
      return receipt;
    };
    window.__nextLanesReleaseAllAndFlush = async () => {
      const frameSequenceBefore = control.frameSequence;
      const receipts = [];
      let renderFrame = control.renderFrame;
      while (control.pending.length) {
        const pendingReads = control.pending.splice(0);
        const batchFrameSequenceBefore = control.frameSequence;
        for (const pending of pendingReads) {
          pending.call.status = 'resolved';
          pending.call.answeredRevision = pending.reply?.detail_revision || null;
          pending.resolve(pending.reply);
        }
        renderFrame = await twoFrameFlush();
        receipts.push(...pendingReads.map(pending => ({
          released: true,
          callId: pending.call.callId,
          laneId: pending.laneId,
          requestedRevision: pending.revision,
          answeredRevision: pending.reply?.detail_revision || null,
          status: pending.call.status,
          frameProcessed: true,
          renderFrame,
          frameSequence: control.frameSequence,
          frameSequenceBefore: batchFrameSequenceBefore,
          frameSequenceAfter: control.frameSequence,
        })));
      }
      control.releaseReceipts.push(...receipts);
      return {
        released: true,
        count: receipts.length,
        frameProcessed: true,
        renderFrame,
        frameSequence: control.frameSequence,
        calls: receipts,
      };
    };
    window.__nextLanesReleaseAll = window.__nextLanesReleaseAllAndFlush;
    control.releaseAll = window.__nextLanesReleaseAllAndFlush;
    window.__nextLanesRebindRendererAndFlush = async () => {
      const rendererCallback = window.__nextLanesBaseRendererCallback;
      if (typeof rendererCallback !== 'function') throw new Error('original renderer lane callback unavailable');
      const previousSubscriptionSequence = window.__nextLanesSubscriptionSequence || 0;
      const previousCallback = window.__nextLanesRendererCallback;
      const replacementCallback = frame => rendererCallback(frame);
      window.cc.onChatStreamFrame(replacementCallback);
      const renderFrame = await twoFrameFlush();
      const subscriptionEvent = window.__nextLanesSubscriptionEvents.at(-1) || null;
      const receipt = {
        replaced: window.__nextLanesRendererCallback === replacementCallback
          && previousCallback !== replacementCallback,
        callbackReplaced: subscriptionEvent?.callbackReplaced === true,
        currentFrameAvailable: subscriptionEvent?.currentFrameAvailable === true,
        previousSubscriptionSequence,
        subscriptionSequence: window.__nextLanesSubscriptionSequence || 0,
        frameSequence: control.frameSequence,
        renderFrame,
        replayedCurrentFrame: !!control.currentFrame,
      };
      control.subscriptionReceipts.push(receipt);
      return receipt;
    };
    window.cc.workLanesShow = async (laneId, options = {}) => {
      const revision = currentRevision(laneId);
      const call = { callId: control.calls.length + 1, laneId, options: { ...options }, revision, status: 'pending' };
      control.calls.push(call);
      if (!revision) { call.status = 'failed'; call.error = 'lane absent from current synthetic inventory'; throw new Error(call.error); }
      if (control.failures.has(laneId)) { call.status = 'failed'; call.error = 'Synthetic lane detail unavailable'; throw new Error(call.error); }
      const rule = control.holdRules.find(item => item.laneId === laneId && item.remaining > 0
        && (item.membersLimit == null || Number(options.membersLimit) === item.membersLimit)
        && (item.goalId == null || (options.goalId || null) === item.goalId)
        && (item.revision == null || revision === item.revision));
      const reply = replyFor(laneId, options, revision);
      if (rule) {
        rule.remaining -= 1;
        return new Promise(resolve => control.pending.push({ laneId, options: { ...options }, revision, reply, resolve, call }));
      }
      call.status = 'resolved';
      call.answeredRevision = reply.detail_revision || null;
      return reply;
    };
    ${frame ? `await window.__nextLanesSetInventory(${json(frame)});` : ''}
    return true;
  })()`);
}

function questionFixtureToken(runtime, producerStreamId) {
  const direct = runtime.fixtureTokens?.[producerStreamId];
  if (direct) return direct;
  if (producerStreamId === UNLISTED_PRODUCER) {
    const parentToken = runtime.fixtureTokens?.[PRODUCER];
    if (parentToken) return crypto.createHash('sha256').update(`voice-producer:${parentToken}`).digest('hex');
  }
  return null;
}

async function seedQuestion(ctx, ownedIds, { key, specId = null, title, options,
  producerStreamId = PRODUCER } = {}) {
  const token = questionFixtureToken(ctx.runtime || {}, producerStreamId);
  if (!ctx.runtime?.fixtureDaemonPort || !token) throw new Error(`isolated web-gate question producer unavailable: ${producerStreamId}`);
  const questionId = `${QUESTION_PREFIX}${String(key || 'fixture').replace(/[^a-zA-Z0-9_-]/g, '_')}`;
  if (!ownedIds.includes(questionId)) ownedIds.push(questionId);
  let owners = QUESTION_OWNERS.get(ownedIds);
  if (!owners) { owners = new Map(); QUESTION_OWNERS.set(ownedIds, owners); }
  owners.set(questionId, producerStreamId);
  const authority = { ...ctx.runtime, fixtureTokens: { ...(ctx.runtime.fixtureTokens || {}), [producerStreamId]: token } };
  const fixture = questionFixture(questionId, specId, {
    producerStreamId,
    title: title || `Synthetic question ${String(key || 'fixture')}`,
    options,
    allowCustom: true,
  });
  const actions = fixture.options.map((option, index) => ({
    kind: 'yes_no',
    action_id: `next-lanes-fixture-choice-${index + 1}`,
    label: option.label,
    choice: index === 0,
    value: { schema_version: 1, question_id: questionId, answer: option.value },
  }));
  const envelope = {
    schema_version: 1,
    question_id: questionId,
    dedup_key: questionId,
    producer_stream_id: producerStreamId,
    title: fixture.title,
    body: fixture.body,
    ...(specId ? { spec_id: specId } : {}),
    response_mode: fixture.response_mode,
    allow_custom: true,
    options: fixture.options,
  };
  const result = await fixtureRequest(authority, producerStreamId, { type: 'prompt.ask', envelope, actions });
  if (result?.ok !== true || result.question?.question_id !== questionId) {
    const refusal = {
      type: result?.type || null,
      error_code: result?.error_code || null,
      message: result?.message || result?.error || null,
      expected_question_id: questionId,
      actual_question_id: result?.question?.question_id || null,
      producer_stream_id: producerStreamId,
      action_count: actions.length,
      option_count: fixture.options.length,
    };
    throw new Error(`isolated synthetic question was not created: ${JSON.stringify(refusal)}`);
  }
  const status = await fixtureRequest(authority, producerStreamId, { type: 'prompt.status', question_id: questionId });
  const durable = status?.question;
  const actualSpec = durable?.spec_id ?? durable?.envelope?.spec_id ?? null;
  const actualAllowCustom = durable?.allow_custom ?? durable?.envelope?.allow_custom ?? null;
  if (status?.ok !== true || durable?.question_id !== questionId || durable?.producer_stream_id !== producerStreamId
    || durable?.state !== 'open' || actualSpec !== (specId || null) || actualAllowCustom !== true) {
    throw new Error(`isolated question durable identity mismatch: ${JSON.stringify({
      questionId,
      statusType: status?.type || null,
      state: durable?.state || null,
      producerStreamId: durable?.producer_stream_id || null,
      expectedProducerStreamId: producerStreamId,
      actualSpec: actualSpec || null,
      expectedSpec: specId || null,
      actualAllowCustom,
      expectedAllowCustom: true,
    })}`);
  }
  return durable;
}

async function fixtureRequestWithPublication(runtime, producerStreamId, message, predicate) {
  const token = questionFixtureToken(runtime || {}, producerStreamId);
  if (!token || !runtime?.fixtureDaemonPort) throw new Error(`isolated publication producer unavailable: ${producerStreamId}`);
  const requestId = message?.request_id;
  if (typeof requestId !== 'string' || !requestId) throw new Error('publication request_id is required');
  if (typeof predicate !== 'function') throw new TypeError('publication predicate is required');
  const authority = { ...runtime, fixtureTokens: { ...(runtime.fixtureTokens || {}), [producerStreamId]: token } };
  const ws = new WebSocket(`ws://127.0.0.1:${authority.fixtureDaemonPort}`);
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => finish(new Error(`isolated publication capture timed out: ${requestId}`)), 10000);
    let sent = false;
    let reply = null;
    let publication = null;
    let settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (ws.readyState === WebSocket.OPEN) ws.close();
      else if (ws.readyState === WebSocket.CONNECTING) ws.terminate();
      error ? reject(error) : resolve(value);
    };
    ws.on('error', error => finish(error));
    ws.on('close', () => {
      if (!settled) finish(new Error(`isolated publication socket closed before completion: ${requestId}`));
    });
    ws.on('open', () => ws.send(JSON.stringify({
      type: 'hello',
      client: 'test',
      from_stream_id: producerStreamId,
      stream_token: token,
      subscribe: { all: true },
      capabilities: { assistant_composite_v1: true },
    })));
    ws.on('message', raw => {
      try {
        const frame = JSON.parse(raw);
        if (frame.type === 'snapshot' && !sent) {
          sent = true;
          ws.send(JSON.stringify(message));
        }
        if (frame.type === 'chat.event' && predicate(frame.event)) publication = frame.event;
        if (frame.request_id === requestId) {
          if (frame.type?.endsWith('.error') || frame.error_code) {
            finish(new Error(`isolated publication refused: ${JSON.stringify({
              type: frame.type || null, error_code: frame.error_code || null, message: frame.message || frame.error || null,
              request_id: requestId,
            })}`));
            return;
          }
          reply = frame;
        }
        if (reply && publication) finish(null, { reply, publication });
      } catch (error) { finish(error); }
    });
  });
}

async function notificationFor(ctx, questionId) {
  const reply = await ctx.session.eval(`window.cc.notificationList({ states: ['open'] })`);
  return Array.isArray(reply?.notifications)
    ? reply.notifications.find(item => item?.question?.question_id === questionId) || null
    : null;
}

async function waitForQuestion(ctx, questionId) {
  const selector = `.next-question-list-item[data-question-id="${questionId}"]`;
  await ctx.session.waitFor(`!!document.querySelector(${json(selector)})`, { timeoutMs: ctx.timeoutMs });
}

async function openQuestionList(ctx) {
  const opened = await ctx.session.eval("document.querySelector('[aria-label^=Questions]')?.classList.contains('open')");
  if (!opened) await ctx.session.click('[aria-label^="Questions"]');
}

async function cleanupQuestions(ctx, ownedIds, report) {
  const errors = [];
  const owners = QUESTION_OWNERS.get(ownedIds) || new Map();
  for (const questionId of [...new Set(ownedIds || [])]) {
    const streamId = owners.get(questionId) || ctx.fixture?.streamId || PRODUCER;
    const token = questionFixtureToken(ctx.runtime || {}, streamId);
    const authority = token ? { ...ctx.runtime, fixtureTokens: { ...(ctx.runtime.fixtureTokens || {}), [streamId]: token } } : ctx.runtime;
    try {
      let status = await fixtureRequest(authority, streamId, { type: 'prompt.status', question_id: questionId });
      if (status?.error_code === 'question_not_found') continue;
      if (status?.ok !== true || status.question?.question_id !== questionId
        || status.question?.producer_stream_id !== streamId) throw new Error(`synthetic question status mismatch: ${questionId}`);
      if (status.question.state === 'open') {
        const cancelled = await fixtureRequest(authority, streamId, { type: 'prompt.cancel', question_id: questionId });
        if (cancelled?.ok !== true && cancelled?.error_code !== 'question_terminal_state') {
          throw new Error(`synthetic question cleanup cancel failed: ${questionId}`);
        }
        status = await fixtureRequest(authority, streamId, { type: 'prompt.status', question_id: questionId });
      }
      if (!['answered', 'consumed', 'dismissed', 'expired'].includes(status.question?.state)) {
        throw new Error(`synthetic question remained open: ${questionId}`);
      }
    } catch (error) { errors.push(error); }
  }
  if (errors.length) {
    report?.note?.(`next-lanes fixture cleanup failed: ${errors.map(error => error.message).join('; ')}`);
    throw new AggregateError(errors, 'next-lanes isolated fixture cleanup failed');
  }
}

async function cleanupHook(ctx, hook) {
  if (!hook) return;
  if (!hook.identifier) throw new Error('new-document hook has no removal identifier');
  await ctx.session.send('Page.removeScriptToEvaluateOnNewDocument', { identifier: hook.identifier });
}

module.exports = {
  PRODUCER,
  QUESTION_PREFIX,
  UNLISTED_PRODUCER,
  cleanupHook,
  cleanupQuestions,
  enterNext,
  fixtureRequestWithPublication,
  installFrameHook,
  installLaneBridge,
  laneDetailVersions,
  notificationFor,
  openQuestionList,
  questionFixtureToken,
  reconnectAndReplaceSubscription,
  seedQuestion,
  waitForQuestion,
};
