'use strict';

const PAGE_SIZE = 32;
const REVISION_RE = /^[a-f0-9]{64}$/;
const CANCELLED = Symbol('cancelled lane read');
// Presence and worked time are outside detail_revision, so without a new
// revision a held or failed read is read again only once it is this old.
const SETTLED_REFRESH_MS = 10 * 60 * 1000;

function laneRevision(lane) {
  return typeof lane?.detail_revision === 'string' ? lane.detail_revision : null;
}

function laneFingerprint(lane) {
  try { return JSON.stringify(lane); } catch { return null; }
}

function readKey(laneId, kind, goalId) {
  return `${laneId}\u0000${kind}\u0000${goalId || ''}`;
}

function protocolError(message = 'Incomplete lane detail reply') {
  const error = new Error(message);
  error.code = 'work_lane_detail_invalid';
  return error;
}

function errorMessage(error) {
  for (const value of [error?.message, error?.error, error?.error_code, error?.code, error]) {
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return 'Lane details unavailable';
}

function createLaneReads({ show, now = () => Date.now() } = {}) {
  if (typeof show !== 'function') throw new TypeError('show must be a function');

  let inventory = null;
  let lanes = new Map();
  const reads = new Map();
  const listeners = new Set();

  function emit() {
    for (const listener of [...listeners]) {
      try { listener(); } catch { /* A subscriber cannot break the read controller. */ }
    }
  }

  function laneFor(laneId) {
    return lanes.get(laneId) || null;
  }

  function entryFor(laneId, kind, goalId = null, create = false) {
    const key = readKey(laneId, kind, goalId);
    let entry = reads.get(key);
    if (!entry && create) {
      entry = {
        key, laneId, kind, goalId,
        active: false,
        goalsDemanded: false,
        status: 'idle',
        reply: null,
        detailRevision: null,
        error: null,
        lane: laneFor(laneId),
        revision: laneRevision(laneFor(laneId)),
        fingerprint: laneFingerprint(laneFor(laneId)),
        waitingReply: null,
        startedRevision: null,
        revisionRestarts: 0,
        token: 0,
        waiter: null,
        settledAt: 0,
      };
      reads.set(key, entry);
    }
    return entry || null;
  }

  function currentReply(entry) {
    const current = laneFor(entry.laneId);
    return entry.reply && entry.detailRevision === laneRevision(current) ? entry.reply : null;
  }

  function stateFor(entry) {
    if (!entry) return { status: 'idle', reply: null, detailRevision: null, error: null };
    const reply = currentReply(entry);
    return {
      status: entry.status,
      reply,
      detailRevision: reply ? entry.detailRevision : null,
      error: entry.error,
    };
  }

  function getRead(laneId, kind, goalId = null) {
    if (!['goals', 'members', 'goal'].includes(kind)) throw new TypeError(`unknown lane read kind: ${kind}`);
    const memberEntry = kind === 'goals' ? entryFor(laneId, 'members') : null;
    const goalEntry = kind === 'goals' ? entryFor(laneId, 'goals') : null;
    if (memberEntry) {
      const memberState = stateFor(memberEntry);
      if (memberState.reply) return memberState;
      const goalState = stateFor(goalEntry);
      if (goalState.reply) return goalState;
      if (memberEntry.goalsDemanded && memberEntry.status !== 'idle') return memberState;
    }
    return stateFor(kind === 'goals' ? goalEntry : entryFor(laneId, kind, goalId));
  }

  function makeWaiter(entry) {
    if (entry.waiter) return entry.waiter.promise;
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    entry.waiter = { promise, resolve };
    return promise;
  }

  function settleWaiter(entry) {
    if (!entry.waiter) return;
    const waiter = entry.waiter;
    entry.waiter = null;
    waiter.resolve(stateFor(entry));
  }

  function fail(entry, error) {
    entry.token += 1;
    entry.waitingReply = null;
    if (!currentReply(entry)) {
      entry.reply = null;
      entry.detailRevision = null;
    }
    entry.status = 'failed';
    entry.error = errorMessage(error);
    entry.settledAt = now();
    settleWaiter(entry);
    emit();
  }

  function hold(entry, reply, revision) {
    entry.reply = reply;
    entry.detailRevision = revision;
    entry.settledAt = now();
    entry.waitingReply = null;
    entry.status = 'held';
    entry.error = null;
    entry.revisionRestarts = 0;
    settleWaiter(entry);
    emit();
  }

  function restart(entry, { countRevision = true, preserveReply = true, resetRestarts = false, error } = {}) {
    if (countRevision && entry.revisionRestarts >= 1) {
      fail(entry, error || protocolError('Lane detail changed more than once while reading'));
      return;
    }
    if (countRevision) entry.revisionRestarts += 1;
    if (resetRestarts) entry.revisionRestarts = 0;
    start(entry, { preserveReply, resetRestarts: false });
  }

  function validateReply(reply, laneId) {
    if (!reply || typeof reply !== 'object') throw protocolError();
    if (typeof reply.type === 'string' && /\.(?:error|failed)$/.test(reply.type)) {
      throw new Error(reply.error || reply.error_code || 'Lane detail request failed');
    }
    if (reply.lane?.lane_id !== laneId || reply.projection?.lane_id !== laneId) throw protocolError();
    if (typeof reply.detail_revision !== 'string' || !REVISION_RE.test(reply.detail_revision)) throw protocolError();
    return reply.detail_revision;
  }

  function validateGoals(reply) {
    const projection = reply.projection;
    if (!Array.isArray(projection.goals) || !Number.isSafeInteger(projection.goals_total)
      || projection.goals_total < 0 || projection.goals.length !== projection.goals_total) {
      throw protocolError('Incomplete lane goals reply');
    }
  }

  function validatePage(reply, laneId, goalId) {
    const projection = reply.projection;
    validateGoals(reply);
    if (goalId && (!projection.selected_goal || projection.selected_goal.goal_id !== goalId)) throw protocolError();

    const members = reply.members;
    const summaryMembers = projection.members;
    const total = reply.members_total;
    const next = reply.members_next;
    if (!Array.isArray(members) || !Array.isArray(summaryMembers)
      || !Number.isSafeInteger(total) || total < 0 || projection.members_total !== total
      || !Object.prototype.hasOwnProperty.call(reply, 'members_next')
      || !Object.prototype.hasOwnProperty.call(projection, 'members_next')
      || projection.members_next !== next
      || !(next === null || typeof next === 'string')) throw protocolError();
    if (members.length !== summaryMembers.length) throw protocolError();
    for (let i = 0; i < members.length; i += 1) {
      const id = members[i]?.spec_id;
      if (typeof id !== 'string' || !id || summaryMembers[i]?.spec_id !== id) throw protocolError();
    }
    return { members, summaryMembers, total, next, revision: reply.detail_revision };
  }

  async function readGoals(entry, token) {
    const reply = await show(entry.laneId, { membersLimit: 1 });
    if (token !== entry.token) return CANCELLED;
    validateReply(reply, entry.laneId);
    validateGoals(reply);
    return reply;
  }

  async function readPages(entry, token) {
    const pages = [];
    const memberIds = new Set();
    const cursors = new Set();
    let after;
    let expectedRevision = null;
    let expectedTotal = null;

    for (;;) {
      const options = { membersLimit: PAGE_SIZE };
      if (entry.kind === 'goal') options.goalId = entry.goalId;
      if (after) options.membersAfter = after;
      const reply = await show(entry.laneId, options);
      if (token !== entry.token) return CANCELLED;
      const revision = validateReply(reply, entry.laneId);
      if (expectedRevision !== null && revision !== expectedRevision) {
        const moved = new Error('Lane detail revision changed between pages');
        moved.revisionChange = true;
        throw moved;
      }
      const page = validatePage(reply, entry.laneId, entry.kind === 'goal' ? entry.goalId : null);
      if (expectedRevision === null) expectedRevision = revision;
      if (expectedTotal !== null && page.total !== expectedTotal) throw protocolError();
      if (expectedTotal === null) expectedTotal = page.total;

      for (let i = 0; i < page.members.length; i += 1) {
        const id = page.members[i].spec_id;
        if (memberIds.has(id) || (i > 0 && id <= page.members[i - 1].spec_id)
          || (i === 0 && after && id <= after)) throw protocolError();
        memberIds.add(id);
      }
      if (memberIds.size > expectedTotal) throw protocolError();
      pages.push(reply);

      if (page.next === null) {
        const members = pages.flatMap(item => item.members);
        const summaryMembers = pages.flatMap(item => item.projection.members);
        if (members.length !== expectedTotal || memberIds.size !== expectedTotal) throw protocolError();
        const first = pages[0];
        const allIds = members.map(member => member.spec_id);
        const explicitPositions = Object.assign({}, ...pages.map(item => item.explicit_member_positions || {}));
        return {
          ...first,
          lane: { ...first.lane, ...(Array.isArray(first.lane.members) ? { members: allIds } : {}) },
          members,
          members_total: expectedTotal,
          members_next: null,
          explicit_member_positions: explicitPositions,
          projection: {
            ...first.projection,
            members: summaryMembers,
            members_total: expectedTotal,
            members_next: null,
          },
        };
      }

      const lastId = page.members[page.members.length - 1]?.spec_id;
      if (!lastId || page.next !== lastId || (after && page.next <= after) || cursors.has(page.next)) throw protocolError();
      cursors.add(page.next);
      after = page.next;
    }
  }

  async function run(entry, token) {
    try {
      const reply = entry.kind === 'goals' ? await readGoals(entry, token) : await readPages(entry, token);
      if (reply === CANCELLED || token !== entry.token) return;
      const revision = reply.detail_revision;
      const currentRevision = laneRevision(entry.lane);
      if (revision === currentRevision) {
        hold(entry, reply, revision);
      } else if (currentRevision !== entry.startedRevision) {
        restart(entry, { countRevision: true, preserveReply: false });
      } else {
        // A hash has no ordering. Keep the checked reply hidden until a frame
        // for its exact revision arrives or another frame supersedes it.
        entry.waitingReply = { reply, revision };
        entry.status = 'loading';
        entry.error = null;
        emit();
      }
    } catch (error) {
      if (token !== entry.token) return;
      if (error?.revisionChange) restart(entry, { countRevision: true, preserveReply: currentReply(entry) !== null, error });
      else fail(entry, error);
    }
  }

  function start(entry, { preserveReply = false, resetRestarts = true } = {}) {
    const lane = laneFor(entry.laneId);
    if (!lane) {
      entry.status = 'idle';
      entry.reply = null;
      entry.detailRevision = null;
      entry.error = null;
      entry.waitingReply = null;
      settleWaiter(entry);
      emit();
      return Promise.resolve(stateFor(entry));
    }
    entry.active = true;
    entry.lane = lane;
    entry.revision = laneRevision(lane);
    entry.fingerprint = laneFingerprint(lane);
    entry.waitingReply = null;
    if (resetRestarts) entry.revisionRestarts = 0;
    if (!preserveReply || entry.detailRevision !== entry.revision) {
      entry.reply = null;
      entry.detailRevision = null;
    }
    entry.status = 'loading';
    entry.error = null;
    entry.startedRevision = entry.revision;
    const waiter = makeWaiter(entry);
    entry.token += 1;
    const token = entry.token;
    emit();
    void run(entry, token);
    return waiter;
  }

  function requestEntry(entry) {
    entry.active = true;
    if (entry.status === 'loading' && entry.waiter) return entry.waiter.promise;
    if (entry.status === 'held' && currentReply(entry)) return Promise.resolve(stateFor(entry));
    if (entry.status === 'failed') return Promise.resolve(stateFor(entry));
    return start(entry, { preserveReply: currentReply(entry) !== null });
  }

  function updateInventory(nextInventory) {
    inventory = nextInventory || null;
    const nextLanes = new Map((Array.isArray(inventory?.lanes) ? inventory.lanes : [])
      .filter(lane => lane && typeof lane.lane_id === 'string')
      .map(lane => [lane.lane_id, lane]));
    lanes = nextLanes;

    for (const entry of reads.values()) {
      const nextLane = laneFor(entry.laneId);
      const nextFingerprint = laneFingerprint(nextLane);
      const nextRevision = laneRevision(nextLane);
      if (!nextLane) {
        if (entry.lane || entry.status !== 'idle' || entry.waitingReply) {
          entry.token += 1;
          entry.lane = null;
          entry.revision = null;
          entry.fingerprint = null;
          entry.reply = null;
          entry.detailRevision = null;
          entry.waitingReply = null;
          entry.status = 'idle';
          entry.error = null;
          entry.revisionRestarts = 0;
          settleWaiter(entry);
          emit();
        }
        continue;
      }

      const oldRevision = entry.revision;
      const changed = entry.fingerprint !== nextFingerprint;
      entry.lane = nextLane;
      entry.revision = nextRevision;
      entry.fingerprint = nextFingerprint;
      if (!changed) continue;

      if (entry.waitingReply) {
        if (entry.waitingReply.revision === nextRevision) {
          hold(entry, entry.waitingReply.reply, entry.waitingReply.revision);
          continue;
        }
        const revisionChanged = oldRevision !== nextRevision;
        restart(entry, {
          countRevision: revisionChanged,
          preserveReply: entry.detailRevision === nextRevision,
          resetRestarts: !revisionChanged,
        });
        continue;
      }

      if (entry.status === 'loading') {
        const revisionChanged = oldRevision !== nextRevision;
        restart(entry, {
          countRevision: revisionChanged,
          preserveReply: !revisionChanged && entry.detailRevision === nextRevision,
          resetRestarts: false,
        });
      } else {
        const revisionChanged = oldRevision !== nextRevision;
        if (revisionChanged) {
          entry.reply = null;
          entry.detailRevision = null;
        }
        // Most pushes change only presence; a read is due on a new revision or an old result.
        const due = revisionChanged || now() - entry.settledAt > SETTLED_REFRESH_MS;
        if (entry.active && due) start(entry, { preserveReply: !revisionChanged && entry.detailRevision === nextRevision });
      }
    }
  }

  function requestGoals(laneId) {
    const memberEntry = entryFor(laneId, 'members');
    if (memberEntry) {
      const memberState = stateFor(memberEntry);
      if (memberState.reply) return Promise.resolve(memberState);
      const goalEntry = entryFor(laneId, 'goals');
      const goalState = stateFor(goalEntry);
      if (goalState.reply) return Promise.resolve(goalState);
      if (memberEntry.status === 'loading' && memberEntry.active) {
        memberEntry.goalsDemanded = true;
        return memberEntry.waiter?.promise || Promise.resolve(memberState);
      }
      if (memberEntry.goalsDemanded && memberEntry.status === 'failed') return Promise.resolve(memberState);
    }
    return requestEntry(entryFor(laneId, 'goals', null, true));
  }

  function requestMembers(laneId) {
    return requestEntry(entryFor(laneId, 'members', null, true));
  }

  function requestGoal(laneId, goalId) {
    if (typeof goalId !== 'string' || !goalId) throw new TypeError('goalId is required');
    return requestEntry(entryFor(laneId, 'goal', goalId, true));
  }

  function retry(laneId, kind, goalId = null) {
    if (!['goals', 'members', 'goal'].includes(kind)) throw new TypeError(`unknown lane read kind: ${kind}`);
    if (kind === 'goals') {
      const memberEntry = entryFor(laneId, 'members');
      if (memberEntry?.goalsDemanded && memberEntry.status === 'failed') {
        return start(memberEntry, { preserveReply: currentReply(memberEntry) !== null });
      }
    }
    const entry = entryFor(laneId, kind, goalId, true);
    return start(entry, { preserveReply: currentReply(entry) !== null });
  }

  function subscribe(listener) {
    if (typeof listener !== 'function') throw new TypeError('listener must be a function');
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  return {
    updateInventory,
    requestGoals,
    requestMembers,
    requestGoal,
    getRead,
    retry,
    subscribe,
    get inventory() { return inventory; },
  };
}

module.exports = { PAGE_SIZE, createLaneReads };
