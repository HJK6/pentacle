'use strict';

const CARD_KEYS = Object.freeze(['chat', 'chat2', 'dash', 'lanes']);
const MIN_WIDTH = Object.freeze({ assistant: 280, chat: 312, chat2: 312, dash: 312, lanes: 356 });
const RAIL_WIDTH = 60;
const PHONE_WIDTH = 600;
const AUTO_LANES_STAGE_WIDTH = 900;
const GAP = 12, EDGE = 44, TOP = 26, STICK = 8, LIFT = 7;
const LANE_DRAWN = 344;
const SEATS = Object.freeze(['assistant', ...CARD_KEYS]);
const SHARE = Object.freeze({ assistant: 420, chat: 420, chat2: 420, dash: 640 });
const AUTO_SHARE = Object.freeze({ assistant: 1, chat: 1, chat2: 1, dash: 2.2 });
const chatKey = key => key === 'chat' || key === 'chat2';
const readRail = value => value === 'top' ? 'top' : 'left';
const roomAt = (width, rail) => Math.max(0, width - GAP - (readRail(rail) === 'left' ? RAIL_WIDTH : 0));


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
  const lanes = roomAt(width, state.rail) >= AUTO_LANES_STAGE_WIDTH;
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
function createLayoutState(width, rail) {
  width = readWidth(width);
  const lanes = roomAt(width, rail) >= AUTO_LANES_STAGE_WIDTH;
  return {
    chat: null,
    chat2: null,
    rail: readRail(rail),
    seats: SEATS.slice(),
    shares: {},
    half: null,
    aside: false,
    overlay: false,
    dash: null,
    lanes,
    autoLanes: true,
    order: lanes ? ['lanes'] : [],
    front: 'assistant',
    last: 'assistant',
    solo: null,
    peek: null,
  };
}

/** The two main panes before lanes and viewport fit are applied. */
function pair(state) {
  if (state.chat && state.chat2) return ['chat', 'chat2'];
  const chat = state.chat ? 'chat' : state.chat2 ? 'chat2' : null;
  if (state.dash) return [chat && (state.front === chat || state.aside) ? chat : 'assistant', 'dash'];
  return ['assistant', ...(chat ? [chat] : [])];
}

/**
 * Project state to visible panes for a viewport width. This is read-only: a
 * narrow desktop can hide old panes and a wider desktop can reveal them again.
 */
function fits(state, width) {
  width = readWidth(width);
  const current = laneDefaultAtWidth(state, width);
  if (width >= PHONE_WIDTH && current.solo && current[current.solo]) {
    return { cards: [current.solo], assistant: false, solo: true };
  }

  let open = current.order.filter(key => current[key]);
  if (width < PHONE_WIDTH) {
    return { cards: open.slice(-1), assistant: open.length === 0 };
  }

  const mainPair = pair(current);
  const assistantIn = mainPair[0] === 'assistant' && !current.aside;
  const room = roomAt(width, current.rail);
  open = open.filter(key => key === 'lanes' || mainPair.includes(key));
  const needed = () => open.reduce((sum, key) => sum + MIN_WIDTH[key], assistantIn ? MIN_WIDTH.assistant : 0);
  while (open.length > 1 && needed() > room) open.shift();

  return {
    cards: CARD_KEYS.filter(key => open.includes(key)),
    assistant: (assistantIn && needed() <= room) || (!open.length && !current.aside),
  };
}

/** Open or replace one card from the rail/list. */
function show(state, key, id, width) {
  assertCardKey(key);
  if (id === undefined || id === null || id === false || id === '') {
    throw new TypeError('an open card needs a truthy id');
  }
  const next = prepareAction(state, width);

  next.overlay = false;
  if (key === 'chat') {
    const primary = next.chat ? 'chat' : next.chat2 ? 'chat2' : 'chat';
    const other = primary === 'chat' ? 'chat2' : 'chat';
    const existing = [primary, other].find(slot => next[slot] === id);
    if (width >= PHONE_WIDTH && existing && fits(next, width).cards.includes(existing)) {
      next.last = existing;
      return next;
    }
    next.solo = null;
    if (next.chat && next.chat2 && width >= PHONE_WIDTH) {
      key = next.last === 'chat2' ? 'chat' : 'chat2';
      if (existing) key = existing;
    } else {
      key = primary;
      if (next.dash && next[primary] && (next.front === primary || next.aside)) {
        if (next.last === primary && width >= PHONE_WIDTH && !existing) {
          key = other;
          next.dash = null;
          next.front = 'assistant';
        }
      } else if (next.dash) {
        if (next.last === 'dash') next.front = key;
        else next.dash = null;
      } else if (width >= PHONE_WIDTH && next[primary] && next.last === primary && !existing) {
        key = other;
      }
    }
    next.peek = null;
  } else if (key === 'dash') {
    if (next.chat && next.chat2) {
      const keep = next.last === 'chat2' ? 'chat2' : 'chat';
      next[keep === 'chat' ? 'chat2' : 'chat'] = null;
      next.last = keep;
    }
    if (!next.dash) next.front = chatKey(next.last) && next[next.last] ? next.last : 'assistant';
    next.peek = null;
  }

  next[key] = key === 'lanes' ? Boolean(id) : id;
  touch(next, key);
  if (next.solo !== key) next.solo = null;
  if (key !== 'lanes') next.last = key;
  return settle(next, width);
}

/** Open a dashboard or picture link beside the pane that contained it. */
function follow(state, source, id, width) {
  if (source !== 'assistant' && !chatKey(source)) throw new TypeError(`unknown link source: ${source}`);
  if (id === undefined || id === null || id === false || id === '') {
    throw new TypeError('a followed card needs a truthy id');
  }
  if (state.dash === id && fits(state, width).cards.includes('dash')) return shut(state, 'dash', width);

  const next = prepareAction(state, width);
  if (source !== 'assistant') next.overlay = false;
  if (next.chat && next.chat2) {
    next[source === 'chat2' || (source === 'assistant' && next.last === 'chat2') ? 'chat' : 'chat2'] = null;
  }
  if (chatKey(source)) {
    next.peek = next.peek || { solo: next.solo === source, source };
    touch(next, source);
  } else {
    next.peek = null;
  }
  next.front = source;
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
    const returnChat = next.peek?.source || 'chat';
    if (key === 'dash' && next.peek?.solo && next[returnChat]) next.solo = returnChat;
    if (next.last === key) {
      const survivor = next.chat ? 'chat' : next.chat2 ? 'chat2' : null;
      next.last = survivor && (chatKey(key) || next.front === survivor) ? survivor : 'assistant';
    }
    next.peek = null;
    next.front = 'assistant';
  }
  if (!CARD_KEYS.some(slot => next[slot])) {
    next.aside = false;
    next.overlay = false;
  }
  return next;
}

/** The brand switches the actual pane; it never closes a desktop chat. */
function toAssistant(state, width) {
  const next = prepareAction(state, width);
  if (width < PHONE_WIDTH) {
    Object.assign(next, { chat: null, chat2: null, dash: null, lanes: false, solo: null,
      front: 'assistant', last: 'assistant', peek: null, aside: false, overlay: false });
  } else if (next.overlay) {
    next.overlay = false;
  } else if (fits(next, width).assistant) {
    next.aside = true;
    if (next.last === 'assistant') next.last = next.chat ? 'chat' : next.chat2 ? 'chat2' : next.dash ? 'dash' : 'assistant';
  } else {
    const returnToStage = { ...next, aside: false };
    if (fits(returnToStage, width).assistant) return { ...returnToStage, last: 'assistant' };
    next.overlay = true;
  }
  return next;
}

function keepAssistant(state, width) {
  const next = prepareAction(state, width);
  if (!next.overlay) return next;
  // Over full screen, Keep exits solo without rearranging its underlying pair.
  if (!next.solo) {
    if (next.chat && next.chat2) next[next.last === 'chat2' ? 'chat' : 'chat2'] = null;
    next.front = 'assistant';
  }
  next.solo = null;
  next.last = 'assistant';
  next.peek = null;
  next.aside = next.overlay = false;
  // A narrow desktop cannot keep even one chat/dashboard beside Assistant.
  // Remove the card bindings, not the sessions, just as on the two-chat path.
  if (width >= PHONE_WIDTH && roomAt(width, next.rail) < MIN_WIDTH.assistant + MIN_WIDTH.chat) {
    next.chat = next.chat2 = next.dash = null;
  }
  return next;
}

const dismissAssistant = state => ({ ...state, overlay: false });
const setRail = (state, value) => ({ ...state, rail: readRail(value) });
function replaceChat(state, slot, id) {
  if (!chatKey(slot) || state[slot] !== '__new__') return state;
  const other = slot === 'chat' ? 'chat2' : 'chat';
  if (state[other] === id) return { ...state, [slot]: null, last: other, solo: state.solo === slot ? null : state.solo };
  return { ...state, [slot]: id, last: slot };
}

/** Toggle a chat/dashboard into or out of full screen. The mock has no such control on phones. */
function toggleFullScreen(state, key, width) {
  if (!chatKey(key) && key !== 'dash') throw new TypeError(`card cannot fill the screen: ${key}`);
  width = readWidth(width);
  const next = prepareAction(state, width);
  if (width < PHONE_WIDTH || !next[key]) return next;
  next.solo = next.solo === key ? null : key;
  return next;
}

/** Record which visible main pane was used last; lanes never affect this choice. */
function markUsed(state, key) {
  if (!SEATS.includes(key)) throw new TypeError(`unknown used pane: ${key}`);
  if (key === 'lanes' || (key === 'assistant' && state.overlay)) return copyState(state);
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
  if (width < PHONE_WIDTH) {
    const keep = fits(next, width).cards;
    for (const key of CARD_KEYS) {
      if (next[key] && !keep.includes(key) && !(next.peek && key === (next.peek.source || 'chat'))) {
        next[key] = key === 'lanes' ? false : null;
      }
    }
  }
  return next;
}

function visibleSeats(state, width) {
  const { assistant, cards } = fits(state, width);
  return state.seats.filter(key => key === 'assistant' ? assistant : cards.includes(key));
}

function canResize(a, b) {
  return !!a && !!b && a !== 'lanes' && b !== 'lanes';
}

function clamp(value, lo, hi) { return Math.max(lo, Math.min(hi, value)); }

// Freeze any proportional allocation at a floor before dividing the rest.
function allocate(keys, room, shares, result) {
  if (!keys.length) return;
  const weight = key => shares[key] || SHARE[key];
  const sum = keys.reduce((n, key) => n + weight(key), 0);
  const floor = keys.find(key => weight(key) * room / sum < MIN_WIDTH[key]);
  if (floor) {
    result[floor] = MIN_WIDTH[floor];
    allocate(keys.filter(key => key !== floor), room - result[floor], shares, result);
  } else {
    for (const key of keys) result[key] = room * weight(key) / sum;
  }
}

function seatGap(seats, stageWidth) {
  if (seats.length < 2) return 0;
  const floors = seats.reduce((sum, key) => sum + (key === 'lanes' ? LANE_DRAWN : MIN_WIDTH[key]), 0);
  return clamp((stageWidth - floors) / (seats.length - 1), 0, GAP);
}

function seatWidths(state, seats, stageWidth) {
  const sizes = {};
  const gap = seatGap(seats, stageWidth);
  let room = stageWidth - Math.max(0, seats.length - 1) * gap;
  if (seats.includes('lanes')) {
    sizes.lanes = LANE_DRAWN;
    room -= LANE_DRAWN;
  }
  const main = seats.filter(key => key !== 'lanes');
  let remaining = main;
  if (seats.length > 1 && main.includes(state.half)) {
    remaining = main.filter(key => key !== state.half);
    const floors = remaining.reduce((sum, key) => sum + MIN_WIDTH[key], 0);
    sizes[state.half] = remaining.length || seats.includes('lanes')
      ? clamp(stageWidth / 2 - (state.half === 'assistant' ? 0 : gap), MIN_WIDTH[state.half], room - floors)
      : room;
    room -= sizes[state.half];
    if (!remaining.length) return sizes;
  }
  const shares = state.shares;
  if (main.includes('assistant') && main.includes('dash') && !main.some(key => shares[key]) && !state.half) {
    sizes.assistant = clamp(stageWidth * 0.26, MIN_WIDTH.assistant, Math.min(380, room - MIN_WIDTH.dash));
    room -= sizes.assistant;
    remaining = remaining.filter(key => key !== 'assistant');
  }
  allocate(remaining, room, main.some(key => shares[key]) ? shares : AUTO_SHARE, sizes);
  return sizes;
}

function resizeBoundary(state, seats, sizes, index, dx) {
  const [a, b] = seats.slice(index, index + 2);
  if (!canResize(a, b)) return state;
  const shares = { ...state.shares };
  for (const key of seats) if (key !== 'lanes') shares[key] = sizes[key];
  const total = sizes[a] + sizes[b];
  shares[a] = clamp(sizes[a] + dx, MIN_WIDTH[a], total - MIN_WIDTH[b]);
  shares[b] = total - shares[a];
  return { ...state, shares, half: null };
}

const resetWidths = state => ({ ...state, shares: {}, half: null });

function positions(order, sizes, start, gap = GAP) {
  const result = {};
  for (const key of order) {
    result[key] = start;
    start += sizes[key] + gap;
  }
  return result;
}

const liftReached = (x, y) => Math.abs(x) + Math.abs(y) >= LIFT;

function aimDrag(drag, x, y) {
  const { key, start, bounds, boxes, sizes } = drag;
  const dx = x - start.x, dy = y - start.y;
  const order = drag.order.slice();
  let type = 'slot';
  if (chatKey(key) || key === 'dash') {
    if (y < bounds.top + TOP && (start.y >= bounds.top + TOP || dy < -10)) type = 'full';
  }
  if (type === 'slot' && x < bounds.left + EDGE && (start.x >= bounds.left + EDGE || dx < -16)) type = 'left';
  if (type === 'slot' && x > bounds.edge - EDGE && (start.x <= bounds.edge - EDGE || dx > 16)) type = 'right';
  if (type === 'left' || type === 'right') {
    order.splice(order.indexOf(key), 1);
    order.splice(type === 'left' ? 0 : order.length, 0, key);
  } else if (type === 'slot') {
    const left = boxes[key].left + dx, right = left + sizes[key];
    // Do not reverse within one pointer sample, even with unequal widths.
    let direction = 0;
    for (let i = 0; i < order.length; i++) {
      const at = positions(order, sizes, bounds.left, drag.gap);
      const j = order.indexOf(key), prev = order[j - 1], next = order[j + 1];
      if (direction >= 0 && next && right > at[next] + sizes[next] / 2 + STICK) {
        [order[j], order[j + 1]] = [next, key];
        direction = 1;
      } else if (direction <= 0 && prev && left < at[prev] + sizes[prev] / 2 - STICK) {
        [order[j - 1], order[j]] = [key, prev];
        direction = -1;
      } else break;
    }
  }
  return { ...drag, order, type, dx, dy };
}

function dropSection(state, drag, width) {
  if (drag.type === 'full') return toggleFullScreen(state, drag.key, width);
  const next = prepareAction(state, width);
  const order = drag.order.slice();
  next.seats = next.seats.map(key => drag.order.includes(key) ? order.shift() : key);
  if ((drag.type === 'left' || drag.type === 'right') && drag.key !== 'lanes') {
    next.half = drag.key;
    next.shares = { ...next.shares, ...Object.fromEntries(Object.entries(drag.sizes).filter(([key]) => key !== 'lanes')) };
  }
  return next;
}

module.exports = {
  AUTO_LANES_STAGE_WIDTH,
  CARD_KEYS,
  MIN_WIDTH,
  PHONE_WIDTH,
  RAIL_WIDTH, GAP, LANE_DRAWN,
  readRail, setRail, roomAt, replaceChat,
  keepAssistant, dismissAssistant,
  visibleSeats, seatWidths, seatGap, canResize, resizeBoundary, resetWidths,
  positions, liftReached, aimDrag, dropSection,
  createLayoutState,
  pair,
  fits,
  show,
  follow,
  shut,
  toAssistant,
  toggleFullScreen,
  markUsed,
  settle,
};
