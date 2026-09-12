const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const { registerAssetIpcHandlers } = require('../main/asset_ipc_bridge');

test('preload asset API forwards renderer calls to asset IPC channels', async () => {
  const calls = [];
  const ipcRenderer = {
    invoke(channel, ...args) {
      calls.push({ channel, args });
      return Promise.resolve({ ok: true, channel, args });
    },
    send() {},
    on() {},
    removeAllListeners() {},
  };
  const context = {
    require(name) {
      if (name === 'electron') return { clipboard: { readText: () => '', writeText() {} }, ipcRenderer };
      if (name === './config-loader') return { loadConfig: () => ({ config: {} }) };
      return require(name);
    },
    window: {},
    process,
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'preload.js'), 'utf8'), context, {
    filename: 'preload.js',
  });

  await context.window.cc.assetList({ stream_id: 'hostb:provider_c-1' });
  await context.window.cc.assetGet({ stream_id: 'hostb:provider_c-1', asset_id: 'asset-1' });
  await context.window.cc.assetCommentsList({ stream_id: 'hostb:provider_c-1', asset_id: 'asset-1' });
  await context.window.cc.assetCommentAdd({ asset_id: 'asset-1', body: 'note' });
  await context.window.cc.assetCommentEdit({ asset_id: 'asset-1', comment_id: 'c1', body: 'edit' });
  await context.window.cc.assetCommentDelete({ asset_id: 'asset-1', comment_id: 'c1' });
  await context.window.cc.assetCommentResolve({ asset_id: 'asset-1', comment_id: 'c1', resolved: true });
  await context.window.cc.assetReviewSet({ asset_id: 'asset-1', review_status: 'approved' });
  await context.window.cc.assetCommentsSendToChat({ asset_id: 'asset-1' });
  await context.window.cc.assetPopOut({ asset_id: 'asset-1' });
  await context.window.cc.assetDock({ asset_id: 'asset-1' });

  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
    { channel: 'chat-stream:asset-list', args: [{ stream_id: 'hostb:provider_c-1' }] },
    { channel: 'chat-stream:asset-get', args: [{ stream_id: 'hostb:provider_c-1', asset_id: 'asset-1' }] },
    { channel: 'chat-stream:asset-comments-list', args: [{ stream_id: 'hostb:provider_c-1', asset_id: 'asset-1' }] },
    { channel: 'chat-stream:asset-comment-add', args: [{ asset_id: 'asset-1', body: 'note' }] },
    { channel: 'chat-stream:asset-comment-edit', args: [{ asset_id: 'asset-1', comment_id: 'c1', body: 'edit' }] },
    { channel: 'chat-stream:asset-comment-delete', args: [{ asset_id: 'asset-1', comment_id: 'c1' }] },
    { channel: 'chat-stream:asset-comment-resolve', args: [{ asset_id: 'asset-1', comment_id: 'c1', resolved: true }] },
    { channel: 'chat-stream:asset-review-set', args: [{ asset_id: 'asset-1', review_status: 'approved' }] },
    { channel: 'chat-stream:asset-comments-send-to-chat', args: [{ asset_id: 'asset-1' }] },
    { channel: 'chat-stream:asset-pop-out', args: [{ asset_id: 'asset-1' }] },
    { channel: 'chat-stream:asset-dock', args: [{ asset_id: 'asset-1' }] },
  ]);
});

test('main asset IPC handlers forward arguments to chatStreamClient', async () => {
  const handlers = new Map();
  const calls = [];
  const ipcMain = {
    handle(channel, handler) {
      handlers.set(channel, handler);
    },
  };
  const chatStreamClient = {
    assetList(args) {
      calls.push(['list', args]);
      return Promise.resolve({ assets: [{ asset_id: 'a1' }] });
    },
    assetGet(args) {
      calls.push(['get', args]);
      return Promise.resolve({ asset: { asset_id: 'a1', body: '# Done' } });
    },
    assetCommentsList(args) {
      calls.push(['comments-list', args]);
      return Promise.resolve({ comments: [] });
    },
    assetCommentAdd(args) {
      calls.push(['comment-add', args]);
      return Promise.resolve({ comment: { comment_id: 'c1' } });
    },
    assetCommentEdit(args) {
      calls.push(['comment-edit', args]);
      return Promise.resolve({ comment: { comment_id: 'c1' } });
    },
    assetCommentDelete(args) {
      calls.push(['comment-delete', args]);
      return Promise.resolve({ comment: { comment_id: 'c1' } });
    },
    assetCommentResolve(args) {
      calls.push(['comment-resolve', args]);
      return Promise.resolve({ comment: { comment_id: 'c1' } });
    },
    assetReviewSet(args) {
      calls.push(['review-set', args]);
      return Promise.resolve({ asset: { asset_id: 'a1', review_status: 'approved' } });
    },
    assetCommentsSendToChat(args) {
      calls.push(['comments-send', args]);
      return Promise.resolve({ unresolved: 1 });
    },
  };
  const assetPopouts = {
    open(args) {
      calls.push(['pop-out', args]);
      return { ok: true, windowId: 7 };
    },
    dock(args) {
      calls.push(['dock', args]);
      return { ok: true, docked: true };
    },
  };

  registerAssetIpcHandlers(ipcMain, chatStreamClient, (error) => String(error.message || error), assetPopouts);

  assert.deepEqual(
    (await handlers.get('chat-stream:asset-list')(null, { stream_id: 'hostb:provider_c-1' })).assets,
    [{ asset_id: 'a1' }],
  );
  assert.deepEqual(
    (await handlers.get('chat-stream:asset-get')(null, { stream_id: 'hostb:provider_c-1', asset_id: 'a1' })).asset,
    { asset_id: 'a1', body: '# Done' },
  );
  assert.deepEqual(
    (await handlers.get('chat-stream:asset-comments-list')(null, { asset_id: 'a1' })).comments,
    [],
  );
  await handlers.get('chat-stream:asset-comment-add')(null, { asset_id: 'a1', body: 'note' });
  await handlers.get('chat-stream:asset-comment-edit')(null, { asset_id: 'a1', comment_id: 'c1', body: 'edit' });
  await handlers.get('chat-stream:asset-comment-delete')(null, { asset_id: 'a1', comment_id: 'c1' });
  await handlers.get('chat-stream:asset-comment-resolve')(null, { asset_id: 'a1', comment_id: 'c1', resolved: true });
  await handlers.get('chat-stream:asset-review-set')(null, { asset_id: 'a1', review_status: 'approved' });
  await handlers.get('chat-stream:asset-comments-send-to-chat')(null, { asset_id: 'a1' });
  await handlers.get('chat-stream:asset-pop-out')(null, { asset_id: 'a1' });
  await handlers.get('chat-stream:asset-dock')(null, { asset_id: 'a1' });
  assert.deepEqual(calls, [
    ['list', { stream_id: 'hostb:provider_c-1' }],
    ['get', { stream_id: 'hostb:provider_c-1', asset_id: 'a1' }],
    ['comments-list', { asset_id: 'a1' }],
    ['comment-add', { asset_id: 'a1', body: 'note' }],
    ['comment-edit', { asset_id: 'a1', comment_id: 'c1', body: 'edit' }],
    ['comment-delete', { asset_id: 'a1', comment_id: 'c1' }],
    ['comment-resolve', { asset_id: 'a1', comment_id: 'c1', resolved: true }],
    ['review-set', { asset_id: 'a1', review_status: 'approved' }],
    ['comments-send', { asset_id: 'a1' }],
    ['pop-out', { asset_id: 'a1' }],
    ['dock', { asset_id: 'a1' }],
  ]);

  // The bridges are registered from the shared cc handler module, against
  // whichever target the transport supplies (ipcMain, or the ws collector).
  const source = fs.readFileSync(path.join(__dirname, '..', 'main', 'cc_handlers.js'), 'utf8');
  assert.match(source, /registerAssetIpcHandlers\(target, chatStreamClient, normalizeChatStreamError, assetPopouts\)/);
});
