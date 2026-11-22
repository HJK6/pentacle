'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { JSDOM } = require('jsdom');
const layout = require('../renderer/next/layout');
const { leavePicture, createPictureCard, pictureFrom, markPictures } = require('../renderer/next/picture');

const open = (state, source, id, width) => layout.follow(state, source, 'picture:' + id, width);
const wide = 1440;

test('thumbnail identities are per chat and occurrence; second click closes and another swaps', () => {
  const { document } = new JSDOM('<div data-stream-id="one"><button data-next-picture="a" aria-label="wide.png"><img></button><button data-next-picture="b" aria-label="tall.png"><img></button></div>').window;
  const transcript = document.querySelector('div');
  const [first, second] = transcript.querySelectorAll('button');
  const a = pictureFrom(transcript, first);
  const b = pictureFrom(transcript, second);
  assert.notEqual(a.id, b.id);
  transcript.dataset.streamId = 'two';
  assert.notEqual(pictureFrom(transcript, first).id, a.id);
  transcript.dataset.streamId = 'one';

  let state = layout.follow(layout.createLayoutState(wide), 'assistant', a.id, wide);
  markPictures(transcript, state.dash);
  assert.deepEqual([first, second].map(el => el.getAttribute('aria-pressed')), ['true', 'false']);
  state = layout.follow(state, 'assistant', b.id, wide);
  assert.equal(state.dash, b.id);
  markPictures(transcript, state.dash);
  assert.deepEqual([first, second].map(el => el.getAttribute('aria-pressed')), ['false', 'true']);
  state = layout.follow(state, 'assistant', b.id, wide);
  assert.equal(state.dash, null);
  markPictures(transcript, state.dash);
  assert.deepEqual([first, second].map(el => el.getAttribute('aria-pressed')), ['false', 'false']);
});

test('picture card names and scales its image; stale work cannot overwrite a replacement or reopen it', async () => {
  const { document } = new JSDOM('<section></section>').window;
  const card = document.querySelector('section');
  const viewer = createPictureCard(card, {
    close: document.createElement('button'), full: document.createElement('button'),
  });
  let finish;
  const slow = viewer.open({ name: 'wide.png' }, () => new Promise(resolve => { finish = resolve; }));
  assert.equal(card.querySelector('.card-title').textContent, 'wide.png');
  await viewer.open({ name: 'tall.png' }, async () => 'data:image/png;base64,aQ==');
  finish('data:image/png;base64,YQ==');
  await slow;
  assert.equal(card.querySelector('.card-title').textContent, 'tall.png');
  assert.equal(card.querySelector('img').getAttribute('src'), 'data:image/png;base64,aQ==');
  const closing = viewer.open({ name: 'stale.png' }, () => new Promise(resolve => { finish = resolve; }));
  viewer.close();
  finish('data:image/png;base64,YQ==');
  await closing;
  assert.equal(card.querySelector('img').hasAttribute('src'), false);
});

test('picture projection keeps equal main panes, independent lanes and return placement', () => {
  const initial = layout.createLayoutState(wide);
  const assistant = open(initial, 'assistant', 'a', wide);
  assert.deepEqual(layout.fits(assistant, wide), { assistant: true, cards: ['dash', 'lanes'] });
  const assistantWidths = layout.seatWidths(assistant, layout.visibleSeats(assistant, wide), layout.roomAt(wide, assistant.rail));
  assert.equal(assistantWidths.assistant, assistantWidths.dash);
  assert.equal(assistantWidths.lanes, layout.LANE_DRAWN);
  const chat = layout.show(initial, 'chat', 'other', wide);
  const picture = open(chat, 'chat', 'b', wide);
  assert.deepEqual(layout.fits(picture, wide), { assistant: false, cards: ['chat', 'dash', 'lanes'] });
  assert.deepEqual(layout.fits(layout.shut(picture, 'dash', wide), wide), { assistant: true, cards: ['chat', 'lanes'] });
  assert.deepEqual(layout.fits(layout.toAssistant(picture, wide), wide), { assistant: true, cards: ['dash', 'lanes'] });
  const solo = layout.toggleFullScreen(chat, 'chat', wide);
  const replaced = open(open(solo, 'chat', 'b', wide), 'chat', 'c', wide);
  assert.equal(layout.shut(replaced, 'dash', wide).solo, 'chat');
  const railChat = layout.show(leavePicture(replaced, wide), 'chat', 'selected-from-rail', wide);
  assert.equal(railChat.dash, null);
  assert.equal(railChat.solo, null);
  assert.equal(railChat.peek, null);
  const design = layout.follow(chat, 'chat', 'design:/next/design', wide);
  assert.equal(leavePicture(design, wide), design);
  const second = layout.show(chat, 'chat', 'second', wide);
  const fromSecond = open(second, 'chat2', 'secondary', wide);
  assert.deepEqual(layout.fits(fromSecond, wide), { assistant: false, cards: ['chat2', 'dash', 'lanes'] });
  assert.equal(layout.shut(fromSecond, 'dash', wide).chat2, 'second');
});

test('compact and phone picture wait-behind preserves the source chat on close', () => {
  for (const width of [1010, 702, 440]) {
    const chat = layout.show(layout.createLayoutState(width), 'chat', 'other', width);
    const state = open(chat, 'chat', 'narrow', width);
    assert.deepEqual(layout.fits(state, width), { assistant: false, cards: ['dash'] });
    const closed = layout.shut(state, 'dash', width);
    assert.equal(closed.chat, 'other');
    assert.equal(layout.fits(closed, width).assistant, width >= layout.PHONE_WIDTH);
    assert.ok(layout.fits(closed, width).cards.includes('chat'));
  }
});
