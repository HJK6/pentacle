'use strict';

const assetRender = require('./asset_render');

const state = {
  args: null,
  item: null,
  streamId: '',
  body: null,
  comments: [],
  // Monotonic guard for async refreshes: every writer (full refresh, comment
  // action, comment-only push) bumps it before its awaits and re-checks after,
  // so stale results are dropped instead of applied out of order.
  gen: 0,
};

function normalizeItem(args) {
  const source = args?.asset && typeof args.asset === 'object' ? args.asset : args || {};
  const sessionKey = source.session_key || args?.session_key || {};
  const assetId = String(source.asset_id || source.assetId || args?.asset_id || args?.assetId || '').trim();
  const streamId = String(args?.stream_id || args?.streamId || sessionKey.stream_id || '').trim();
  return {
    asset_id: assetId,
    title: source.title || args?.title || assetId,
    content_type: source.content_type || source.contentType || args?.content_type || 'unknown',
    review_status: source.review_status || source.reviewStatus || args?.review_status || 'pending_review',
    spec_id: source.spec_id || source.specId || args?.spec_id || null,
    updated_at: source.updated_at || source.updatedAt || args?.updated_at || '',
    session_key: {
      host: sessionKey.host || args?.host || '',
      session_name: sessionKey.session_name || args?.session_name || args?.sessionName || '',
      stream_id: streamId,
    },
  };
}

function assetRpcArgs() {
  const sessionKey = state.item?.session_key || {};
  const args = { stream_id: state.streamId, asset_id: state.item?.asset_id };
  if (sessionKey.host) args.host = sessionKey.host;
  if (sessionKey.session_name) args.session_name = sessionKey.session_name;
  if (state.item?.spec_id) args.spec_id = state.item.spec_id;
  return args;
}

function payloadForRender(contentType, body) {
  if (contentType === 'json_table' && typeof body === 'string') {
    try { return JSON.parse(body); } catch (_) { return { columns: [], rows: [] }; }
  }
  if (contentType === 'report' && typeof body === 'string') {
    try { return JSON.parse(body); } catch (_) { return body; }
  }
  return body;
}

async function listComments() {
  if (!window.cc?.assetCommentsList || state.item?.content_type !== 'report') return [];
  const reply = await window.cc.assetCommentsList(assetRpcArgs());
  return Array.isArray(reply?.comments) ? reply.comments : [];
}

function reportActions() {
  const call = async (method, args) => {
    const reply = await window.cc[method]({ ...assetRpcArgs(), ...(args || {}) });
    if (reply?.ok === false) throw new Error(reply.error || `${method} failed`);
    const metadataChanged = !!reply?.asset;
    if (metadataChanged) state.item = normalizeItem({ ...state.args, asset: reply.asset, stream_id: state.streamId });
    const gen = ++state.gen;
    const comments = await listComments();
    // Stale-guard: a later writer (full refresh, republish push, newer comment
    // refetch) superseded this result — it owns comment state.
    if (state.gen !== gen) return reply;
    state.comments = comments;
    // Comment mutations (no asset in the reply) update the mounted report in
    // place so the popout keeps its DOM and scroll; metadata-bearing replies
    // (review status) re-render to refresh the toolbar.
    if (metadataChanged || !assetRender.updateReportComments(state.item?.asset_id, comments)) render();
    return reply;
  };
  return {
    addComment(comment) {
      return call('assetCommentAdd', {
        section_id: comment.section_id,
        block_id: comment.block_id,
        run_index: comment.run_index,
        excerpt: comment.excerpt,
        body: comment.body,
      });
    },
    editComment(commentId, body) {
      return call('assetCommentEdit', { comment_id: commentId, body });
    },
    deleteComment(commentId) {
      return call('assetCommentDelete', { comment_id: commentId });
    },
    resolveComment(commentId, resolved) {
      return call('assetCommentResolve', { comment_id: commentId, resolved });
    },
    sendToChat() {
      return call('assetCommentsSendToChat');
    },
    setReviewStatus(reviewStatus) {
      return call('assetReviewSet', { review_status: reviewStatus });
    },
  };
}

async function refresh() {
  if (!state.item?.asset_id || !window.cc?.assetGet) return;
  const gen = ++state.gen;
  const reply = await window.cc.assetGet(assetRpcArgs());
  const asset = reply?.asset && typeof reply.asset === 'object' ? reply.asset : null;
  if (state.gen !== gen) return;
  if (asset) state.item = normalizeItem({ ...state.args, asset, stream_id: state.streamId });
  state.body = asset && Object.prototype.hasOwnProperty.call(asset, 'body') ? asset.body : reply?.body;
  const comments = await listComments();
  if (state.gen !== gen) return;
  state.comments = comments;
  render();
}

function render() {
  const title = document.getElementById('asset-popout-title');
  const meta = document.getElementById('asset-popout-meta');
  const content = document.getElementById('asset-popout-content');
  if (!state.item || !content) return;
  document.title = state.item.title || state.item.asset_id;
  title.textContent = state.item.title || state.item.asset_id;
  meta.textContent = [state.item.content_type, state.item.review_status].filter(Boolean).join(' / ');
  content.innerHTML = '';
  content.appendChild(assetRender.renderAsset(
    document,
    state.item.content_type,
    payloadForRender(state.item.content_type, state.body),
    {
      classPrefix: 'slot-asset',
      asset: state.item,
      comments: state.comments,
      reviewStatus: state.item.review_status,
      actions: state.item.content_type === 'report' ? reportActions() : null,
    },
  ));
}

function sameAssetUpdateForItem(item, streamId, payload) {
  if (!payload || payload.type !== 'asset.update' || !item) return false;
  if (String(payload.asset_id || '') !== item.asset_id) return false;
  const itemSpecId = String(item.spec_id || '').trim();
  const payloadSpecId = String(payload.spec_id || '').trim();
  if (itemSpecId || payloadSpecId) return !!itemSpecId && itemSpecId === payloadSpecId;
  const sessionKey = payload.session_key || {};
  return String(sessionKey.stream_id || '') === streamId
    || (
      String(sessionKey.host || '') === String(item.session_key.host || '')
      && String(sessionKey.session_name || '') === String(item.session_key.session_name || '')
    );
}

function sameAssetUpdate(payload) {
  return sameAssetUpdateForItem(state.item, state.streamId, payload);
}

function sameAssetRemovedForItem(item, streamId, payload) {
  if (!payload || payload.type !== 'asset.removed' || !item) return false;
  if (String(payload.asset_id || '') !== item.asset_id) return false;
  const itemSpecId = String(item.spec_id || '').trim();
  const payloadSpecId = String(payload.spec_id || '').trim();
  if (itemSpecId || payloadSpecId) return !!itemSpecId && itemSpecId === payloadSpecId;
  const sessionKey = payload.session_key || {};
  return String(sessionKey.stream_id || '') === streamId
    || (
      String(sessionKey.host || '') === String(item.session_key.host || '')
      && String(sessionKey.session_name || '') === String(item.session_key.session_name || '')
    );
}

function sameAssetRemoved(payload) {
  return sameAssetRemovedForItem(state.item, state.streamId, payload);
}

function init(args) {
  state.args = args || {};
  state.item = normalizeItem(state.args);
  state.streamId = state.item.session_key.stream_id;
  render();
  refresh().catch((error) => {
    const content = document.getElementById('asset-popout-content');
    if (content) content.textContent = `Asset failed: ${error?.message || error}`;
  });
}

if (typeof document !== 'undefined' && typeof window !== 'undefined') {
  document.getElementById('asset-popout-dock')?.addEventListener('click', async () => {
    if (!state.item || !window.cc?.assetDock) return;
    await window.cc.assetDock({ ...state.args, stream_id: state.streamId, asset_id: state.item.asset_id, asset: state.item });
  });

  window.cc?.onAssetPopoutInit?.((payload) => init(payload));
  window.cc?.onChatStreamFrame?.((payload) => {
    if (sameAssetRemoved(payload)) {
      try { window.close(); } catch (_) { /* ignore */ }
      return;
    }
    if (!sameAssetUpdate(payload)) return;
    const prevUpdatedAt = String(state.item?.updated_at || '');
    state.item = normalizeItem({ ...state.args, asset: { ...state.item, ...payload }, stream_id: state.streamId });
    // Comment mutations broadcast asset.update without bumping updated_at
    // (republish and review-status changes do bump it): refresh comments in
    // place, keeping the popout DOM, scroll, and cached body. A changed
    // updated_at keeps the full re-fetch (revision republish contract).
    if (prevUpdatedAt && String(payload.updated_at || '') === prevUpdatedAt) {
      const gen = ++state.gen;
      listComments().then((comments) => {
        // Stale-guard: skip if a later writer superseded this refetch mid-flight.
        if (state.gen !== gen) return;
        state.comments = comments;
        if (!assetRender.updateReportComments(state.item?.asset_id, comments)) render();
      }).catch(() => {});
      return;
    }
    state.body = null;
    refresh().catch(() => render());
  });
}

if (typeof module !== 'undefined') {
  module.exports = {
    sameAssetUpdateForItem,
    sameAssetRemovedForItem,
  };
}
