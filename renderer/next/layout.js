'use strict';

const CARD_KEYS = Object.freeze(['chat', 'dash', 'lanes']);
const MIN_WIDTH = Object.freeze({ assistant: 280, chat: 312, dash: 312, lanes: 356 });
const RAIL_WIDTH = 60;
const PHONE_WIDTH = 600;
const AUTO_LANES_STAGE_WIDTH = 900;

function readWidth(width) {
  if (!Number.isFinite(width) || width < 0) {
    throw new RangeError('width must be a finite, non-negative number');
  }
  return width;
}

function assertCardKey(key) {
  if (!CARD_KEYS.includes(key)) throw new TypeError(`unknown layout card: ${key}`);
}

function copyState(state) {
  if (!state || typeof state !== 'object') throw new TypeError('layout state is required');
  return {
    ...state,
    order: Array.isArray(state.order) ? state.order.slice() : [],
    peek: state.peek ? { ...state.peek } : null,
  };
}

function laneDefaultAtWidth(state, width) {
  if (!state.autoLanes) return state;
  const lanes = width >= RAIL_WIDTH + AUTO_LANES_STAGE_WIDTH;
  const order = lanes
    ? ['lanes', ...state.order.filter(key => key !== 'lanes')]
    : state.order.filter(key => key !== 'lanes');
  return { ...state, lanes, order };
}

function prepareAction(state, width) {
  const next = copyState(laneDefaultAtWidth(state, readWidth(width)));
  next.autoLanes = false;
  return next;
}

function touch(state, key) {
  state.order = state.order.filter(item => item !== key).concat(key);
}

/**
 * Create a fresh layout. Until its first layout action, lanes follow the mock's
 * room default: open when the stage beside the 60 px rail is at least 900 px.
 */
function createLayoutState(width) {
  width = readWidth(width);
  const lanes = width >= RAIL_WIDTH + AUTO_LANES_STAGE_WIDTH;
  return {
    chat: null,
    dash: null,
    lanes,
    autoLanes: true,
    order: lanes ? ['lanes'] : [],
    front: 'assistant',
    last: 'assistant',
    solo: null,
    peek: null,
    // null leaves the dashboard's CSS clamp in control; a drag stores pixels.
    assistantWidth: null,
  };
}

/** The two main panes before lanes and viewport fit are applied. */
function pair(state) {
  if (state.dash && state.chat) {
    return [state.front === 'chat' ? 'chat' : 'assistant', 'dash'];
  }
  return ['assistant', ...(state.chat ? ['chat'] : state.dash ? ['dash'] : [])];
}

/**
 * Project state to visible panes for a viewport width. This is read-only: a
 * narrow desktop can hide old panes and a wider desktop can reveal them again.
 */
function fits(state, width) {
  width = readWidth(width);
  const current = laneDefaultAtWidth(state, width);
  if (current.solo && current[current.solo]) {
    return { cards: [current.solo], assistant: false, solo: true };
  }

  let open = current.order.filter(key => current[key]);
  if (width < PHONE_WIDTH) {
    return { cards: open.slice(-1), assistant: open.length === 0 };
  }

  const mainPair = pair(current);
  const assistantIn = mainPair[0] === 'assistant';
  const room = width - RAIL_WIDTH;
  open = open.filter(key => key === 'lanes' || mainPair.includes(key));
  const needed = () => open.reduce((sum, key) => sum + MIN_WIDTH[key], assistantIn ? MIN_WIDTH.assistant : 0);
  while (open.length > 1 && needed() > room) open.shift();

  return {
    cards: CARD_KEYS.filter(key => open.includes(key)),
    assistant: (assistantIn && needed() <= room) || open.length === 0,
  };
}

/** Open or replace one card from the rail/list. */
function show(state, key, id, width) {
  assertCardKey(key);
  if (id === undefined || id === null || id === false || id === '') {
    throw new TypeError('an open card needs a truthy id');
  }
  const next = prepareAction(state, width);

  if (key === 'chat') {
    // With a dashboard beside Assistant, the new chat takes Assistant's place if the
    // dashboard was used last; otherwise it replaces the dashboard.
    if (next.dash && !(next.chat && next.front === 'chat')) {
      if (next.last === 'dash') next.front = 'chat';
      else next.dash = null;
    }
    next.peek = null;
  }
  if (key === 'dash') {
    if (!next.dash) next.front = next.chat && next.last === 'chat' ? 'chat' : 'assistant';
    next.peek = null;
  }

  next[key] = key === 'lanes' ? Boolean(id) : id;
  touch(next, key);
  if (next.solo !== key) next.solo = null;
  if (key !== 'lanes') next.last = key;
  return settle(next, width);
}

/** Store a dragged Assistant width while leaving card minimums available. */
function setAssistantWidth(state, requestedWidth, stageWidth, viewportWidth) {
  viewportWidth = readWidth(viewportWidth);
  if (!Number.isFinite(requestedWidth)) throw new RangeError('requested Assistant width must be finite');
  if (!Number.isFinite(stageWidth) || stageWidth < 0) throw new RangeError('stage width must be finite and non-negative');

  const next = prepareAction(state, viewportWidth);
  const visible = fits(next, viewportWidth);
  if (!visible.assistant || visible.cards.length === 0) {
    throw new Error('Assistant width can only be dragged beside a visible card');
  }
  const cardsWidth = visible.cards.reduce((sum, key) => sum + MIN_WIDTH[key], 0);
  const maxWidth = stageWidth - cardsWidth;
  if (maxWidth < MIN_WIDTH.assistant) throw new RangeError('stage is too narrow for the visible cards');
  next.assistantWidth = Math.round(Math.max(MIN_WIDTH.assistant, Math.min(maxWidth, requestedWidth)));
  return next;
}

/** Open a dashboard or picture link beside the pane that contained it. */
function follow(state, source, id, width) {
  if (source !== 'assistant' && source !== 'chat') throw new TypeError(`unknown link source: ${source}`);
  if (id === undefined || id === null || id === false || id === '') {
    throw new TypeError('a followed card needs a truthy id');
  }
  if (state.dash === id && fits(state, width).cards.includes('dash')) {
    return shut(state, 'dash', width);
  }

  const next = prepareAction(state, width);
  if (source === 'chat') {
    next.peek = next.peek || { solo: next.solo === 'chat' };
    touch(next, 'chat');
  } else {
    next.peek = null;
  }
  next.front = source === 'chat' ? 'chat' : 'assistant';
  next.dash = id;
  touch(next, 'dash');
  next.solo = null;
  next.last = 'dash';
  return settle(next, width);
}

/** Close a card. A linked chat returns after its dashboard or picture closes. */
function shut(state, key, width) {
  assertCardKey(key);
  const next = prepareAction(state, width);
  next[key] = key === 'lanes' ? false : null;
  if (next.solo === key) next.solo = null;

  if (key !== 'lanes') {
    if (key === 'dash' && next.peek && next.peek.solo && next.chat) next.solo = 'chat';
    if (next.last === key) {
      next.last = key === 'dash' && next.chat && next.front === 'chat' ? 'chat' : 'assistant';
    }
    next.peek = null;
    next.front = 'assistant';
  }
  return next;
}

/** Apply the logo's three behaviors: leave full screen, restore Assistant by a dashboard, or reset to Assistant. */
function toAssistant(state, width) {
  width = readWidth(width);
  const next = prepareAction(state, width);
  if (width >= PHONE_WIDTH && next.solo) {
    next.solo = null;
  } else if (width >= PHONE_WIDTH && next.dash && next.chat && next.front === 'chat') {
    next.front = 'assistant';
  } else {
    next.chat = null;
    next.dash = null;
    next.solo = null;
    next.front = 'assistant';
    if (width < PHONE_WIDTH) next.lanes = false;
  }
  next.peek = null;
  next.last = 'assistant';
  return next;
}

/** Toggle a chat/dashboard into or out of full screen. The mock has no such control on phones. */
function toggleFullScreen(state, key, width) {
  if (key !== 'chat' && key !== 'dash') throw new TypeError(`card cannot fill the screen: ${key}`);
  width = readWidth(width);
  const next = prepareAction(state, width);
  if (width < PHONE_WIDTH || !next[key]) return next;
  next.solo = next.solo === key ? null : key;
  return next;
}

/** Record which visible main pane was used last; lanes never affect this choice. */
function markUsed(state, key) {
  if (!['assistant', 'chat', 'dash', 'lanes'].includes(key)) throw new TypeError(`unknown used pane: ${key}`);
  if (key === 'lanes') return copyState(state);
  const next = copyState(state);
  next.last = key;
  return next;
}

/**
 * Apply phone cleanup after an open/follow action. Desktop projection never
 * discards state, so hidden cards can return when the viewport grows.
 */
function settle(state, width) {
  width = readWidth(width);
  const next = copyState(state);
  if (width < PHONE_WIDTH && !next.solo) {
    const keep = fits(next, width).cards;
    for (const key of CARD_KEYS) {
      if (next[key] && !keep.includes(key) && !(key === 'chat' && next.peek)) {
        next[key] = key === 'lanes' ? false : null;
      }
    }
  }
  return next;
}

module.exports = {
  AUTO_LANES_STAGE_WIDTH,
  CARD_KEYS,
  MIN_WIDTH,
  PHONE_WIDTH,
  RAIL_WIDTH,
  createLayoutState,
  pair,
  fits,
  show,
  follow,
  shut,
  toAssistant,
  toggleFullScreen,
  markUsed,
  setAssistantWidth,
  settle,
};
