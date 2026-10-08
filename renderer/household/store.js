'use strict';
// One in-memory timeline shared by both boards, ported from mobile Personal. A view
// detaches its DOM subscription, never a check deadline or mutation readback. No disk cache.
const CHECK_WINDOW_MS = 5000;
const READBACK_DELAY_MS = 6000;
const UNRESOLVED_NOTICE = "Couldn't confirm — checking again";
const NOT_SAVED_NOTICE = 'Not saved';
const errorCode = error => typeof error?.error_code === 'string' ? error.error_code
  : error?.code === 'disconnected' ? 'disconnected' : undefined;
const isUnknown = error => errorCode(error) === undefined
  || ['unknown_outcome', 'timed_out', 'disconnected'].includes(errorCode(error));
const without = (record, key) => { const next = { ...record }; delete next[key]; return next; };

// Clock injection keeps the same production singleton testable without a global reset API.
function createHouseholdStore(clock = {}) {
  const now = clock.now || (() => Date.now()); // elapsed undo time only, never calendar days
  const later = clock.setTimeout || setTimeout;
  const cancel = clock.clearTimeout || clearTimeout;
  const listeners = new Set();
  const checkTimers = new Map();
  let readGeneration = 0;
  let household = async () => ({ ok: false, error_code: 'unavailable' });
  let state = { status: 'loading', snapshot: null, month: undefined, pending: {},
    hiddenItems: {}, hiddenEvents: {}, notice: null, unresolved: 0, monthLocked: false, eventDate: null, selectionRevision: 0 };
  const getState = () => state;
  const setState = partial => { state = { ...state, ...partial }; listeners.forEach(listener => listener()); };
  const subscribe = listener => { listeners.add(listener); return () => listeners.delete(listener); };
  const sleep = ms => new Promise(resolve => later(resolve, ms));
  function configure(context) {
    if (typeof context?.household === 'function') household = context.household;
  }
  async function request(verb, fields) {
    const result = await household(verb, fields);
    if (!result || result.ok !== true) throw result;
    return result.reply;
  }
  async function read() {
    const generation = ++readGeneration;
    try {
      const frame = await request('household.snapshot', state.month === undefined ? {} : { month: state.month });
      const snapshot = { today: frame.today, month: frame.month, people: frame.people,
        lists: frame.lists, events: frame.events, server_now: frame.server_now };
      if (generation === readGeneration) {
        setState({ status: 'ready', snapshot, notice: state.notice?.kind === 'error' ? null : state.notice });
      }
      return { snapshot, result: { ok: true, server_now: snapshot.server_now } };
    } catch (error) {
      const code = errorCode(error) || 'unknown_outcome';
      if (generation === readGeneration) setState({ status: code === 'unauthorized' ? 'unauthorized' : 'unavailable' });
      return { snapshot: null, result: { error: code } };
    }
  }
  // Exactly one bounded bridge attempt; retry timers belong to mutate(), never the shell poller.
  async function refresh() { return (await read()).result; }
  async function load(month) {
    if (arguments.length && state.monthLocked) return { error: 'month_locked' };
    if (arguments.length) setState({ month });
    return refresh();
  }
  async function mutate(send, committed) {
    try { await send(); }
    catch (error) {
      if (!isUnknown(error)) {
        const code = errorCode(error);
        setState({ notice: { text: NOT_SAVED_NOTICE, kind: 'error' },
          ...(['unauthorized', 'unavailable'].includes(code) ? { status: code } : {}) });
        return 'failed';
      }
      setState({ unresolved: state.unresolved + 1, notice: { text: UNRESOLVED_NOTICE, kind: 'unresolved' } });
      await read();
      let snapshot = null;
      // Mobile's first read never resolves the action. Wait six seconds from its completion,
      // then require a successful fresh snapshot; failed reads cannot decide from stale rows.
      while (!snapshot) { await sleep(READBACK_DELAY_MS); snapshot = (await read()).snapshot; }
      setState({ unresolved: Math.max(0, state.unresolved - 1) });
      if (committed(snapshot)) {
        if (state.unresolved === 0 && state.notice?.kind === 'unresolved') setState({ notice: null });
        return 'saved';
      }
      setState({ notice: { text: NOT_SAVED_NOTICE, kind: 'error' } });
      return 'not_saved';
    }
    await read();
    return 'ok';
  }
  const itemAbsent = itemId => snapshot => !Object.values(snapshot.lists).some(items => items.some(item => item.id === itemId));
  async function finishItem(itemId, send) {
    if (state.hiddenItems[itemId]) return 'ignored';
    setState({ hiddenItems: { ...state.hiddenItems, [itemId]: true } });
    const outcome = await mutate(send, itemAbsent(itemId));
    setState({ hiddenItems: without(state.hiddenItems, itemId) });
    return outcome;
  }
  function checkItem(itemId) {
    if (state.hiddenItems[itemId]) return;
    if (state.pending[itemId] !== undefined) {
      cancel(checkTimers.get(itemId)); checkTimers.delete(itemId);
      setState({ pending: without(state.pending, itemId) }); return;
    }
    const deadline = now() + CHECK_WINDOW_MS;
    setState({ pending: { ...state.pending, [itemId]: deadline } });
    checkTimers.set(itemId, later(() => {
      checkTimers.delete(itemId);
      if (state.pending[itemId] !== deadline) return;
      setState({ pending: without(state.pending, itemId) });
      void finishItem(itemId, () => request('household.item.done', { item_id: itemId }));
    }, CHECK_WINDOW_MS));
  }
  function removeItem(itemId) {
    // A remove supersedes the pending client-side check, preventing a second mutation at 5 s.
    if (state.pending[itemId] !== undefined) {
      cancel(checkTimers.get(itemId)); checkTimers.delete(itemId);
      setState({ pending: without(state.pending, itemId) });
    }
    return finishItem(itemId, () => request('household.item.remove', { item_id: itemId }));
  }
  function addItem(list, text) {
    const label = text.trim();
    const before = state.snapshot?.lists[list] || [];
    if (!label || before.some(item => item.label === label)) return Promise.resolve('ignored');
    const known = new Set(before.map(item => item.id));
    return mutate(() => request('household.item.add', { list, label }),
      snapshot => (snapshot.lists[list] || []).some(item => item.label === label && !known.has(item.id)));
  }
  async function eventMutation(date, run) {
    if (state.monthLocked) return 'ignored';
    // The event's own month is the reconciliation domain, even when its date was
    // typed outside the viewed month. Supersede earlier reads before sending.
    readGeneration++;
    setState({ monthLocked: true, month: date.slice(0, 7), eventDate: date,
      selectionRevision: state.selectionRevision + 1 });
    try { return await run(); }
    finally { setState({ monthLocked: false }); }
  }
  function addEvent(value) {
    const known = new Set((state.snapshot?.events || []).map(event => event.id));
    return eventMutation(value.date, () => mutate(() => request('household.event.add', {
      date: value.date, time: value.time, title: value.title, who: value.who,
    }), snapshot => snapshot.events.some(event => !known.has(event.id) && event.date === value.date && event.title === value.title)));
  }
  async function removeEvent(eventId) {
    if (state.hiddenEvents[eventId] || state.monthLocked) return 'ignored';
    const existing = state.snapshot?.events.find(event => event.id === eventId);
    if (!existing) return 'ignored';
    setState({ hiddenEvents: { ...state.hiddenEvents, [eventId]: true } });
    const outcome = await eventMutation(existing.date, () => mutate(() => request('household.event.remove', { event_id: eventId }),
      snapshot => !snapshot.events.some(event => event.id === eventId)));
    setState({ hiddenEvents: without(state.hiddenEvents, eventId) });
    return outcome;
  }
  return { configure, getState, subscribe, attach: subscribe, detach: listener => listeners.delete(listener),
    load, refresh, addItem, checkItem, removeItem, addEvent, removeEvent };
}
const householdStore = createHouseholdStore();
module.exports = { householdStore, createHouseholdStore, CHECK_WINDOW_MS, READBACK_DELAY_MS,
  UNRESOLVED_NOTICE, NOT_SAVED_NOTICE, isUnknown };
