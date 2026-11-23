'use strict';

// Start the real new-web entry with a synthetic chat/bridge boundary so this
// independently exercises delegated clicks when a browser isn't available.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { createRequire } = require('node:module');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');

const entry = path.resolve(__dirname, '../renderer/next/entry.js');
const realRequire = createRequire(entry);
const src = fs.readFileSync(entry, 'utf8');
const ids = { assistant: 'fixture:assistant', first: 'fixture:one', second: 'fixture:two' };

function start({ decode = async () => {} } = {}) {
  const dom = new JSDOM('<!doctype html><main id="app"></main>', {
    url: 'https://fixture.invalid/next/', runScripts: 'outside-only', pretendToBeVisual: true,
  });
  const { window } = dom;
  window.innerWidth = 1440;
  window.Map = Map;
  window.fetch = async () => { throw new Error('offline shell makes no network requests'); };
  window.HTMLImageElement.prototype.decode = decode;
  const fetched = [];
  const noop = () => {};
  const sessions = [
    { stream_id: ids.assistant, session_kind: 'assistant_composite', title: 'Assistant' },
    { stream_id: ids.first, title: 'First' }, { stream_id: ids.second, title: 'Second' },
  ];
  window.cc = {
    onPtyData: noop, onChatStreamFrame: noop,
    requestStreamEvents: async () => ({ ok: true }),
    killPty: async () => ({}), promptList: async () => ({ ok: true, questions: [] }),
    getChatStreamState: async () => ({ connected: false }),
    chatFetchBlob: async key => {
      fetched.push(key);
      return { ok: true, content_b64: 'YWJj' };
    },
  };
  window.PentacleChatStore = {
    getState: () => ({ sessions }), getLimits: () => [], selectSessionDetail: id => id,
    applyFrame: noop, subscribe: noop, sendTurn: noop,
  };
  window.PentacleChatCore = {
    emptyWorkLanesInventory: () => ({ lanes: [] }), applyWorkLanesFrame: old => old,
    buildDesignPath: () => '/next/design',
  };
  window.PentacleChatView = {
    renderTranscriptTimelineHtml: (_id, _chrome, options) => {
      const data = options.pictureCards ? 'data-next-picture="event-wide"' : '';
      return `<div class="slot-chat-media-grid"><button type="button" class="slot-chat-media-button" ${data} aria-label="wide.png" data-attachment-key="${'a'.repeat(64)}" data-attachment-mime="image/png"><img data-needs-blob="1"><span class="slot-chat-media-loading">Loading image</span></button></div>`;
    },
    hydrateFileAttachments: async () => {},
  };
  let closes = 0;
  const modules = {
    '../web_cc': { installWebCc: noop },
    '../sidebar_filter': { filterSidebarSessions: rows => rows },
    '../workspace_layout': { uniqueAssistantComposite: rows => rows[0] },
    './lane_reads': { createLaneReads: () => ({ subscribe: noop }) },
    './questions': { createQuestions: () => ({ subscribe: noop, update: noop }) },
    './question_notifications': { createQuestionNotifications: () => ({ frame: noop, refreshIdentities: async () => {} }) },
    './lanes': { mountLanesCard: () => ({ render: noop }) },
    './design_viewer': {
      createDesignViewer(card, control) {
        card.innerHTML = '<header class="design-bar"><button class="design-full"></button><button aria-label="Close Design"></button></header>';
        card.querySelector('[aria-label="Close Design"]').onclick = control.onClose;
        return {
          open: () => { control.onNavigate(null); },
          close: () => { closes++; },
          handleKey: () => false,
        };
      },
    },
  };
  window.require = name => name.startsWith('@xterm/') ? {} : (modules[name] || realRequire(name));
  vm.runInContext(src, dom.getInternalVMContext(), { filename: entry });
  const find = selector => window.document.querySelector(selector);
  const click = selector => {
    const el = find(selector);
    assert.ok(el, selector);
    el.click();
  };
  const chat = id => { click('.rail [aria-label="Chats"]'); click(`.popover [data-stream-id="${id}"]`); };
  return { dom, find, click, chat, fetched, closes: () => closes };
}

test('ordinary thumbnail opens a card in the real delegated click handler', async () => {
  const { dom, find, click } = start();
  try {
    await new Promise(resolve => setImmediate(resolve));
    click('.pane .slot-chat-media-button');
    assert.ok(find('.picture-card:not([hidden])'), 'click did not open a picture card');
    assert.equal(find('.picture-title').textContent, 'wide.png');
    assert.equal(find('.pane .slot-chat-media-button').getAttribute('aria-pressed'), 'true');
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(find('.picture-image').getAttribute('src'), 'data:image/png;base64,YWJj');
    click('.pane .slot-chat-media-button');
    assert.ok(find('.picture-card[hidden]'));
  } finally {
    dom.window.close();
  }
});

test('the picture shares the thumbnail fetch and a failed decode retains an honest status', async () => {
  let rejectDecode;
  const decode = () => new Promise((_resolve, reject) => { rejectDecode = reject; });
  const { dom, find, click, fetched } = start({ decode });
  try {
    await new Promise(resolve => setImmediate(resolve));
    click('.pane .slot-chat-media-button');
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(fetched, ['a'.repeat(64)]);
    assert.equal(find('.pane .slot-chat-media-loading').textContent, 'Loading image');
    rejectDecode(new Error('decode failed'));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(find('.pane .slot-chat-media-loading').textContent, 'Image unavailable');
    assert.ok(find('.pane .slot-chat-media-button.is-error'));
  } finally {
    dom.window.close();
  }
});

test('real entry clears pictures and Design on switching, and rail exits a picture peek', async () => {
  const { dom, find, click, chat, closes } = start();
  try {
    await new Promise(resolve => setImmediate(resolve));
    click('.rail [aria-label="Dashboards"]');
    click('.popover [aria-label="Design"]');
    assert.ok(find('.design-card:not([hidden])'));
    assert.equal(dom.window.location.pathname, '/next/design');
    click('.pane .slot-chat-media-button');
    assert.ok(find('.design-card[hidden]'));
    assert.equal(closes(), 1);
    assert.equal(dom.window.location.pathname, '/next/');
    click('.rail [aria-label="Dashboards"]');
    click('.popover [aria-label="Design"]');
    assert.ok(find('.picture-card[hidden]'));
    assert.equal(find('.pane .slot-chat-media-button').getAttribute('aria-pressed'), 'false');
    click('.design-card [aria-label="Close Design"]');
    chat(ids.first);
    click('.chat-card:not([hidden]) .card-head .fullscreen');
    click('.chat-card:not([hidden]) .thread-inner .slot-chat-media-button');
    assert.ok(find('.pane[hidden]'));
    chat(ids.second);
    assert.ok(find('.picture-card[hidden]'));
    assert.ok(find('.pane:not([hidden])'));
    assert.ok(find(`.chat-card:not([hidden]) .thread-inner[data-stream-id="${ids.second}"]`));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(find('.picture-image').hasAttribute('src'), false);
  } finally {
    dom.window.close();
  }
});
