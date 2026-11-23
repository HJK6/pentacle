'use strict';

function createImageAttachments(fetchBlob) {
  const sources = new WeakMap();

  function source(button) {
    if (button.dataset.viewerSrc) return Promise.resolve(button.dataset.viewerSrc);
    if (sources.has(button)) return sources.get(button);
    const { attachmentKey: key, attachmentMime: mime } = button.dataset;
    if (!/^[a-f0-9]{64}$/.test(key) || !['image/png', 'image/jpeg'].includes(mime)) {
      return Promise.reject(new Error('Invalid image attachment'));
    }
    const request = Promise.resolve().then(() => fetchBlob(key)).then(response => {
      if (!response?.ok || typeof response.content_b64 !== 'string' || !response.content_b64) {
        throw new Error('Image fetch failed');
      }
      return `data:${mime};base64,${response.content_b64}`;
    }).catch(error => {
      sources.delete(button);
      throw error;
    });
    sources.set(button, request);
    return request;
  }

  async function hydrateImage(img) {
    const button = img.closest('.slot-chat-media-button');
    const status = button.querySelector('.slot-chat-media-loading');
    img.removeAttribute('data-needs-blob');
    try {
      const resolved = await source(button);
      if (!img.isConnected) return;
      img.src = resolved;
      await img.decode();
      if (!img.isConnected) return;
      button.dataset.viewerSrc = resolved;
      status?.remove();
    } catch {
      sources.delete(button);
      if (!img.isConnected) return;
      button.classList.add('is-error');
      if (status) status.textContent = 'Image unavailable';
    }
  }

  function hydrate(root) {
    for (const img of root.querySelectorAll('.slot-chat-media-button img[data-needs-blob="1"]')) {
      void hydrateImage(img);
    }
  }

  return { source, hydrate };
}

module.exports = { createImageAttachments };
