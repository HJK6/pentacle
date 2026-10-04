'use strict';

const MAX_BYTES = 25 * 1024 * 1024;
const FILE_TYPES = new Set(['application/pdf', 'application/zip', 'model/3mf', 'model/stl', 'model/step', 'application/x-openscad']);

// One instance per renderer. URLs are scoped to mounted nodes, never globally
// reused by hash across conversations or credential changes.
function createFileDownloads({ fetchBlob, urlApi = globalThis.URL, cryptoApi = globalThis.crypto, decode = globalThis.atob, BlobClass = globalThis.Blob } = {}) {
  const records = new Map();
  let observer;
  function revoke(node, record) {
    record.disposed = true;
    if (record.url) urlApi.revokeObjectURL(record.url);
    node.removeAttribute('href');
    if (record.onClick) node.removeEventListener('click', record.onClick);
    if (record.onKey) node.removeEventListener('keydown', record.onKey);
    records.delete(node);
  }
  function prune() {
    for (const [node, record] of records) if (!node.isConnected) revoke(node, record);
    if (!records.size && observer) { observer.disconnect(); observer = undefined; }
  }
  function watch(document) {
    if (!observer && document.defaultView?.MutationObserver) {
      observer = new document.defaultView.MutationObserver(prune);
      observer.observe(document.body, { childList: true, subtree: true });
    }
  }
  async function load(node, record) {
    const status = node.parentElement?.querySelector('.slot-chat-file-status');
    const key = node.dataset.attachmentKey || '';
    const mime = node.dataset.attachmentMime || '';
    const size = Number(node.dataset.attachmentSize);
    record.state = 'loading';
    node.dataset.fileState = 'loading';
    node.setAttribute('aria-disabled', 'true');
    if (status) status.textContent = 'Loading file';
    try {
      if (!/^[0-9a-f]{64}$/.test(key) || !FILE_TYPES.has(mime) || !Number.isSafeInteger(size) || size <= 0 || size > MAX_BYTES) throw new Error('metadata_invalid');
      const response = await fetchBlob(key); // Existing authenticated bridge only.
      if (!response?.ok) {
        const code = response?.error_code || response?.code;
        const error = new Error('fetch_failed');
        error.unavailable = code === 'blob_unknown';
        throw error;
      }
      if (typeof response.content_b64 !== 'string' || response.content_b64.length > Math.ceil(MAX_BYTES / 3) * 4) throw new Error('bytes_invalid');
      const binary = decode(response.content_b64);
      if (binary.length !== size) throw new Error('size_mismatch');
      const bytes = Uint8Array.from(binary, c => c.charCodeAt(0));
      const hash = Array.from(new Uint8Array(await cryptoApi.subtle.digest('SHA-256', bytes)), n => n.toString(16).padStart(2, '0')).join('');
      if (hash !== key) throw new Error('digest_mismatch');
      if (record.disposed || !node.isConnected) return;
      // Download only. No PDF/CAD/archive embedding or active-content preview.
      record.url = urlApi.createObjectURL(new BlobClass([bytes], { type: 'application/octet-stream' }));
      node.href = record.url;
      node.setAttribute('aria-disabled', 'false');
      node.dataset.fileState = record.state = 'ready';
      if (status) status.textContent = `${size} bytes · Download`;
    } catch (error) {
      if (record.disposed || !node.isConnected) return;
      node.removeAttribute('href');
      const unavailable = error?.unavailable === true;
      node.dataset.fileState = record.state = unavailable ? 'unavailable' : 'failed';
      if (status) status.textContent = unavailable ? 'File expired or unavailable' : 'Download failed · click filename to retry';
    }
  }
  async function hydrate(root) {
    if (!root || typeof fetchBlob !== 'function') return;
    prune();
    for (const node of root.querySelectorAll('.slot-chat-file-download')) {
      if (records.has(node)) continue;
      const record = { state: 'loading', url: null, disposed: false };
      records.set(node, record);
      watch(node.ownerDocument);
      record.onClick = event => {
        if (record.state === 'ready' && !record.disposed) return;
        event.preventDefault();
        if (record.state === 'failed') void load(node, record);
      };
      record.onKey = event => {
        if (record.state === 'failed' && (event.key === 'Enter' || event.key === ' ')) {
          event.preventDefault(); node.click();
        }
      };
      node.addEventListener('click', record.onClick);
      node.addEventListener('keydown', record.onKey);
      await load(node, record);
    }
  }
  function dispose() {
    for (const [node, record] of records) revoke(node, record);
    observer?.disconnect(); observer = undefined;
  }
  return { hydrate, dispose };
}
module.exports = { createFileDownloads, MAX_BYTES };
