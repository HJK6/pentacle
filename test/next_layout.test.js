'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
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
} = require('../renderer/next/layout');

const view = (state, width) => fits(state, width);

const cases = [
  {
    journey: 'compact: lanes start open only when there is room',
    run() {
      const belowThreshold = createLayoutState(959);
      const atThreshold = createLayoutState(960);
      assert.deepEqual(view(belowThreshold, 959), { cards: [], assistant: true });
      assert.deepEqual(view(atThreshold, 960), { cards: ['lanes'], assistant: true });

      // The automatic choice follows resize until the first layout action.
      assert.deepEqual(view(belowThreshold, 1300), { cards: ['lanes'], assistant: true });
      const touched = show(belowThreshold, 'chat', 'chat-a', 959);
      assert.equal(touched.autoLanes, false);
      assert.deepEqual(view(touched, 1300), { cards: ['chat'], assistant: true });
    },
  },
  {
    journey: 'compact: Assistant and lanes sit side by side at a readable width',
    run() {
      const state = show(createLayoutState(702), 'lanes', true, 702);
      assert.deepEqual(view(state, 702), { cards: ['lanes'], assistant: true });
      assert.ok(702 - 60 >= 280 + 356);
    },
  },
  {
    journey: 'compact: a chat joins Assistant and lanes, then lanes tuck away when room is short',
    run() {
      let state = createLayoutState(1200);
      state = show(state, 'chat', 'chat-a', 1200);
      assert.deepEqual(view(state, 1200), { cards: ['chat', 'lanes'], assistant: true });
      assert.deepEqual(view(state, 960), { cards: ['chat'], assistant: true });
      assert.deepEqual(view(state, 1200), { cards: ['chat', 'lanes'], assistant: true });
      assert.equal(state.lanes, true, 'narrowing hides lanes without closing their state');
    },
  },
  {
    journey: 'desktop: one other chat at a time',
    run() {
      let state = show(createLayoutState(1440), 'chat', 'chat-a', 1440);
      state = show(state, 'chat', 'chat-b', 1440);
      assert.equal(state.chat, 'chat-b');
      assert.deepEqual(pair(state), ['assistant', 'chat']);
      assert.deepEqual(view(state, 1440), { cards: ['chat', 'lanes'], assistant: true });
    },
  },
  {
    journey: 'desktop: a dashboard opens beside the chat he used last',
    run() {
      let state = show(createLayoutState(1440), 'chat', 'chat-a', 1440);
      state = markUsed(state, 'chat');
      state = show(state, 'dash', 'dashboard-a', 1440);
      assert.equal(state.front, 'chat');
      assert.deepEqual(pair(state), ['chat', 'dash']);
      assert.deepEqual(view(state, 1440), { cards: ['chat', 'dash', 'lanes'], assistant: false });
    },
  },
  {
    journey: 'desktop: two clicks switch the dashboard in its existing slot',
    run() {
      let state = show(createLayoutState(1440), 'dash', 'dashboard-a', 1440);
      state = show(state, 'dash', 'dashboard-b', 1440);
      assert.equal(state.dash, 'dashboard-b');
      assert.equal(state.order.filter(key => key === 'dash').length, 1);
      assert.deepEqual(pair(state), ['assistant', 'dash']);
    },
  },
  {
    journey: 'desktop: opened after he used Assistant, the dashboard stays beside Assistant',
    run() {
      let state = show(createLayoutState(1440), 'chat', 'chat-a', 1440);
      state = markUsed(state, 'assistant');
      state = show(state, 'dash', 'dashboard-a', 1440);
      assert.equal(state.chat, 'chat-a', 'the other chat remains open while it steps aside');
      assert.deepEqual(pair(state), ['assistant', 'dash']);
      assert.deepEqual(view(state, 1440), { cards: ['dash', 'lanes'], assistant: true });
    },
  },
  {
    journey: 'desktop: a chat opened while the dashboard was last used takes Assistant’s place',
    run() {
      let state = show(createLayoutState(1440), 'dash', 'dashboard-a', 1440);
      state = markUsed(state, 'dash');
      state = show(state, 'chat', 'chat-a', 1440);
      assert.equal(state.dash, 'dashboard-a');
      assert.equal(state.front, 'chat');
      assert.deepEqual(pair(state), ['chat', 'dash']);
      assert.deepEqual(view(state, 1440), { cards: ['chat', 'dash', 'lanes'], assistant: false });
    },
  },
  {
    journey: 'desktop: a chat opened after he used Assistant takes the dashboard’s place',
    run() {
      let state = show(createLayoutState(1440), 'dash', 'dashboard-a', 1440);
      state = markUsed(state, 'assistant');
      state = show(state, 'chat', 'chat-a', 1440);
      assert.equal(state.dash, null);
      assert.equal(state.chat, 'chat-a');
      assert.deepEqual(pair(state), ['assistant', 'chat']);
      assert.deepEqual(view(state, 1440), { cards: ['chat', 'lanes'], assistant: true });
    },
  },
  {
    journey: 'desktop: a dashboard link from Assistant opens beside him',
    run() {
      const state = follow(createLayoutState(1440), 'assistant', 'picture-or-dashboard', 1440);
      assert.deepEqual(pair(state), ['assistant', 'dash']);
      assert.deepEqual(view(state, 1440), { cards: ['dash', 'lanes'], assistant: true });
    },
  },
  {
    journey: 'desktop: a second click on the same link closes the dashboard',
    run() {
      let state = follow(createLayoutState(1440), 'assistant', 'dashboard-a', 1440);
      state = follow(state, 'assistant', 'dashboard-a', 1440);
      assert.equal(state.dash, null);
      assert.deepEqual(view(state, 1440), { cards: ['lanes'], assistant: true });
    },
  },
  {
    journey: 'desktop: a picture linked in another chat opens beside it and Assistant steps aside',
    run() {
      let state = show(createLayoutState(1440), 'chat', 'chat-a', 1440);
      state = follow(state, 'chat', 'picture-a', 1440);
      assert.deepEqual(pair(state), ['chat', 'dash']);
      assert.deepEqual(view(state, 1440), { cards: ['chat', 'dash', 'lanes'], assistant: false });
      assert.equal(state.peek.solo, false);
    },
  },
  {
    journey: 'desktop: a rail-opened chat ends the link-follow state',
    run() {
      let state = show(createLayoutState(1440), 'chat', 'chat-a', 1440);
      state = follow(state, 'chat', 'picture-a', 1440);
      state = show(state, 'chat', 'chat-b', 1440);
      assert.equal(state.peek, null);
      assert.equal(state.chat, 'chat-b');
      assert.equal(state.dash, 'picture-a', 'desktop keeps the second card beside the replacement chat');
      assert.deepEqual(pair(state), ['chat', 'dash']);
    },
  },
  {
    journey: 'desktop: closing the picture brings Assistant back beside the chat',
    run() {
      let state = show(createLayoutState(1440), 'chat', 'chat-a', 1440);
      state = follow(state, 'chat', 'picture-a', 1440);
      state = shut(state, 'dash', 1440);
      assert.deepEqual(pair(state), ['assistant', 'chat']);
      assert.deepEqual(view(state, 1440), { cards: ['chat', 'lanes'], assistant: true });
      assert.equal(state.peek, null);
    },
  },
  {
    journey: 'desktop: a picture opened from a full-screen chat returns to full screen when closed',
    run() {
      let state = show(createLayoutState(1440), 'chat', 'chat-a', 1440);
      state = toggleFullScreen(state, 'chat', 1440);
      assert.deepEqual(view(state, 1440), { cards: ['chat'], assistant: false, solo: true });
      state = follow(state, 'chat', 'picture-a', 1440);
      assert.equal(state.peek.solo, true);
      state = shut(state, 'dash', 1440);
      assert.equal(state.solo, 'chat');
      assert.deepEqual(view(state, 1440), { cards: ['chat'], assistant: false, solo: true });
    },
  },
  {
    journey: 'desktop: the logo brings Assistant in beside a dashboard and the chat steps aside',
    run() {
      let state = show(createLayoutState(1440), 'chat', 'chat-a', 1440);
      state = show(state, 'dash', 'dashboard-a', 1440);
      state = toAssistant(state, 1440);
      assert.equal(state.chat, 'chat-a');
      assert.equal(state.front, 'assistant');
      assert.deepEqual(pair(state), ['assistant', 'dash']);
      assert.deepEqual(view(state, 1440), { cards: ['dash', 'lanes'], assistant: true });

      state = shut(state, 'dash', 1440);
      assert.deepEqual(pair(state), ['assistant', 'chat']);
      assert.deepEqual(view(state, 1440), { cards: ['chat', 'lanes'], assistant: true });
    },
  },
  {
    journey: 'desktop: the logo leaves full screen, then returns to Assistant and leaves lanes',
    run() {
      let state = show(createLayoutState(1440), 'chat', 'chat-a', 1440);
      state = toggleFullScreen(state, 'chat', 1440);
      state = toAssistant(state, 1440);
      assert.equal(state.solo, null);
      assert.equal(state.chat, 'chat-a', 'the first logo action only leaves full screen');
      assert.deepEqual(view(state, 1440), { cards: ['chat', 'lanes'], assistant: true });

      state = toAssistant(state, 1440);
      assert.equal(state.chat, null);
      assert.equal(state.lanes, true);
      assert.deepEqual(view(state, 1440), { cards: ['lanes'], assistant: true });
    },
  },
  {
    journey: 'compact: a dashboard opens beside the chat and lanes return when room widens',
    run() {
      let state = show(createLayoutState(1200), 'chat', 'chat-a', 1200);
      state = show(state, 'dash', 'dashboard-a', 1200);
      const narrow = view(state, 1000);
      assert.deepEqual(narrow, { cards: ['chat', 'dash'], assistant: false });
      assert.deepEqual(view(state, 1200), { cards: ['chat', 'dash', 'lanes'], assistant: false });
      assert.equal(state.lanes, true);
      assert.deepEqual(state.order, ['lanes', 'chat', 'dash']);
    },
  },
  {
    journey: 'desktop: Assistant steps aside at 600 px and returns when the stage is wide enough',
    run() {
      const state = show(createLayoutState(800), 'chat', 'chat-a', 800);
      assert.deepEqual(view(state, 600), { cards: ['chat'], assistant: false });
      assert.deepEqual(view(state, 652), { cards: ['chat'], assistant: true });
      assert.equal(state.chat, 'chat-a', 'resize projection does not close the chat');
    },
  },
  {
    journey: 'phone: choosing a chat shows it alone and closing returns to Assistant',
    run() {
      let state = show(createLayoutState(440), 'chat', 'chat-a', 440);
      assert.deepEqual(view(state, 440), { cards: ['chat'], assistant: false });
      state = shut(state, 'chat', 440);
      assert.deepEqual(view(state, 440), { cards: [], assistant: true });
    },
  },
  {
    journey: 'phone: a dashboard or picture fills the screen',
    run() {
      const state = show(createLayoutState(440), 'dash', 'picture-a', 440);
      assert.deepEqual(view(state, 440), { cards: ['dash'], assistant: false });
      assert.equal(state.dash, 'picture-a');
    },
  },
  {
    journey: 'phone: lanes fill the screen',
    run() {
      const state = show(createLayoutState(440), 'lanes', true, 440);
      assert.deepEqual(view(state, 440), { cards: ['lanes'], assistant: false });
    },
  },
  {
    journey: 'phone: open a second thing then close it returns to Assistant, not the earlier card',
    run() {
      let state = show(createLayoutState(440), 'chat', 'chat-a', 440);
      state = show(state, 'dash', 'picture-b', 440);
      assert.equal(state.chat, null, 'opening the second phone card closes the first');
      state = shut(state, 'dash', 440);
      assert.deepEqual(view(state, 440), { cards: [], assistant: true });
    },
  },
  {
    journey: 'phone: following a link from a chat returns to that chat when the link closes',
    run() {
      let state = show(createLayoutState(440), 'chat', 'chat-a', 440);
      state = follow(state, 'chat', 'picture-a', 440);
      assert.equal(state.chat, 'chat-a', 'the followed chat waits behind its link');
      assert.deepEqual(view(state, 440), { cards: ['dash'], assistant: false });
      state = shut(state, 'dash', 440);
      assert.deepEqual(view(state, 440), { cards: ['chat'], assistant: false });
    },
  },
  {
    journey: 'phone: a chat opened from the rail ends the link state',
    run() {
      let state = show(createLayoutState(440), 'chat', 'chat-a', 440);
      state = follow(state, 'chat', 'picture-a', 440);
      state = show(state, 'chat', 'chat-b', 440);
      assert.equal(state.peek, null);
      assert.equal(state.chat, 'chat-b');
      assert.equal(state.dash, null, 'rail-opened chat does not preserve a linked card on phone');
      assert.deepEqual(view(state, 440), { cards: ['chat'], assistant: false });
    },
  },
  {
    journey: 'phone: the logo returns to Assistant and closes lanes too',
    run() {
      let state = show(createLayoutState(440), 'chat', 'chat-a', 440);
      state = follow(state, 'chat', 'picture-a', 440);
      state = show(state, 'lanes', true, 440);
      assert.equal(state.lanes, true);
      state = toAssistant(state, 440);
      assert.equal(state.chat, null);
      assert.equal(state.dash, null);
      assert.equal(state.lanes, false);
      assert.deepEqual(view(state, 440), { cards: [], assistant: true });
    },
  },
  {
    journey: 'phone: 599 px is single-pane; 600 px is desktop projection',
    run() {
      let phone = show(createLayoutState(599), 'chat', 'chat-a', 599);
      phone = show(phone, 'dash', 'picture-a', 599);
      assert.equal(phone.chat, null, 'phone opening closes the earlier card');
      assert.deepEqual(view(phone, 599), { cards: ['dash'], assistant: false });

      let desktop = show(createLayoutState(600), 'chat', 'chat-a', 600);
      desktop = show(desktop, 'dash', 'picture-a', 600);
      assert.equal(desktop.chat, 'chat-a', 'desktop projection hides state instead of closing it');
      assert.deepEqual(view(desktop, 600), { cards: ['dash'], assistant: false });
    },
  },
];

for (const scenario of cases) {
  test(`U1 mirrors ${scenario.journey}`, scenario.run);
}

test('U1 projects every card kind through the same generic slots', () => {
  for (const [key, id] of [['chat', 'chat-a'], ['dash', 'picture-a'], ['lanes', true]]) {
    const state = show(createLayoutState(1440), key, id, 1440);
    assert.ok(view(state, 1440).cards.includes(key), `${key} is visible`);
  }
});

test('layout transitions are pure and focus does not reorder cards', () => {
  const initial = createLayoutState(1200);
  const before = structuredClone(initial);
  const withChat = show(initial, 'chat', 'chat-a', 1200);
  assert.deepEqual(initial, before);
  assert.deepEqual(withChat.order, ['lanes', 'chat']);

  const afterFocus = markUsed(withChat, 'assistant');
  assert.deepEqual(afterFocus.order, withChat.order);
  assert.equal(afterFocus.last, 'assistant');
  assert.equal(withChat.last, 'chat');

  const beforeProjection = structuredClone(initial);
  fits(initial, 440);
  settle(initial, 440);
  assert.deepEqual(initial, beforeProjection);
});

test('dragged Assistant width respects its floor and visible card minimums', () => {
  let state = show(createLayoutState(1440), 'dash', 'dashboard-a', 1440);
  assert.equal(state.assistantWidth, null, 'the shell can use the CSS clamp until a drag occurs');
  state = setAssistantWidth(state, 1100, 1200, 1440);
  assert.equal(state.assistantWidth, 532, 'Assistant stops before the dashboard and lanes minimum widths');
  state = setAssistantWidth(state, 40, 1200, 1440);
  assert.equal(state.assistantWidth, 280);
  assert.throws(() => setAssistantWidth(createLayoutState(800), 400, 740, 800), /visible card/);
});

test('full screen has no phone action and only chat or dashboard can use it', () => {
  const phone = show(createLayoutState(599), 'chat', 'chat-a', 599);
  assert.equal(toggleFullScreen(phone, 'chat', 599).solo, null);
  assert.throws(() => toggleFullScreen(phone, 'lanes', 599), /cannot fill the screen/);
});

test('invalid widths and card keys fail at the pure module boundary', () => {
  assert.throws(() => createLayoutState(Number.NaN), /width must be/);
  assert.throws(() => fits(createLayoutState(800), -1), /width must be/);
  assert.throws(() => show(createLayoutState(800), 'question', true, 800), /unknown layout card/);
});
