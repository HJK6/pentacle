'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const { createShellBridge } = require('../renderer/next/shell_bridge');
const { createSeenMarks } = require('../renderer/next/chat_status');

function fixture(version = 1) {
  const calls = [];
  const views = [];
  const frames = [];
  const callbacks = {};
  const transport = {
    version,
    onNavigate: callback => { callbacks.navigate = callback; },
    onShowing: callback => { callbacks.showing = callback; },
    ready: counts => calls.push(['ready', counts]),
    setAttention: counts => calls.push(['attention', counts]),
  };
  let repaints = 0;
  const bridge = createShellBridge(transport, {
    navigate: view => views.push(view), repaint: () => repaints++, nextFrame: work => frames.push(work),
  });
  bridge?.bind();
  return {
    bridge, callbacks, views, calls,
    paint: () => { for (const work of frames.splice(0)) work(); return repaints; },
  };
}

test('ordinary web and unknown versions do not register a shell or publish counts', () => {
  assert.equal(createShellBridge(undefined, {}), null);
  const unknown = fixture(2);
  assert.equal(unknown.bridge, null);
  assert.deepEqual(unknown.calls, []);
  assert.deepEqual(unknown.callbacks, {});
});

test('readiness waits for the first inventory paint and includes unchanged zero counts', () => {
  const { bridge, calls } = fixture();
  bridge.publish({ chats: 0, assistant: 0 });
  assert.deepEqual(calls, []);
  bridge.receiveInventory();
  bridge.publish({ questions: 0 });
  assert.deepEqual(calls, []);
  bridge.publish({ chats: 0, assistant: 0 });
  bridge.publish({ chats: 0, assistant: 0, questions: 0 });
  bridge.publish({ chats: 2, assistant: 1 });
  bridge.publish({ questions: 3 });
  bridge.publish({ questions: 3 });
  assert.deepEqual(calls, [
    ['ready', { version: 1, chats: 0, assistant: 0, questions: 0 }],
    ['attention', { chats: 2, assistant: 1, questions: 0 }],
    ['attention', { chats: 2, assistant: 1, questions: 3 }],
  ]);
});

test('only the exposed chat is marked seen after the ordered showing signal paints', () => {
  const { bridge, callbacks, views, paint } = fixture();
  bridge.receiveInventory();
  const seen = createSeenMarks();
  const assistant = { stream_id: 'assistant', last_kind: 'ASSIST_TEXT', last_text: 'old Assistant reply' };
  const chat = { stream_id: 'chat', last_kind: 'ASSIST_TEXT', last_text: 'old chat reply' };
  const marks = (active, hidden = false, popover = null) =>
    seen.update([assistant, chat], new Set([active]), bridge.canMarkSeen(hidden, popover), true);

  marks('assistant', true);
  bridge.publish({ chats: 0, assistant: 0 });
  assistant.last_text = 'new Assistant reply';
  chat.last_text = 'new chat reply';
  marks('assistant');
  assert.equal(seen.unread(assistant), true);
  assert.equal(seen.unread(chat), true);

  callbacks.navigate('chats');
  callbacks.showing(true);
  assert.equal(paint(), 1);
  marks('assistant', false, 'chats');
  marks('chat', false, 'questions');
  assert.equal(seen.unread(assistant), true);
  assert.equal(seen.unread(chat), true);
  marks('chat');
  assert.equal(seen.unread(assistant), true);
  assert.equal(seen.unread(chat), false);

  callbacks.showing(false);
  callbacks.navigate('assistant');
  callbacks.showing(true);
  assert.equal(bridge.canMarkSeen(false, null), false);
  assert.equal(paint(), 2);
  marks('assistant', true);
  assert.equal(seen.unread(assistant), true);
  marks('assistant');
  assert.equal(seen.unread(assistant), false);
  assert.deepEqual(views, ['chats', 'assistant']);
});

test('a hide or newer navigation invalidates a pending presentation before it paints', () => {
  const { bridge, callbacks, paint } = fixture();
  bridge.receiveInventory();
  bridge.publish({ chats: 0, assistant: 0 });
  callbacks.navigate('assistant');
  callbacks.showing(true);
  callbacks.showing(false);
  assert.equal(paint(), 0);
  assert.equal(bridge.canMarkSeen(false, null), false);
  callbacks.showing(true);
  callbacks.navigate('questions');
  assert.equal(paint(), 0);
  assert.equal(bridge.canMarkSeen(false, null), false);
});
