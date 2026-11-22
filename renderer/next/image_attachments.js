'use strict';

async function hydrateImage(img, fetchBlob) {
  const button = img.closest('.slot-chat-media-button');
  const status = button.querySelector('.slot-chat-media-loading');
  const { attachmentKey: key, attachmentMime: mime } = button.dataset;
  img.removeAttribute('data-needs-blob');
  try {
    if (!/^[a-f0-9]{64}$/.test(key) || !['image/png', 'image/jpeg'].includes(mime)) {
      throw new Error('Invalid image attachment');
    }
    const response = await fetchBlob(key);
    if (!img.isConnected) return;
    if (!response?.ok || typeof response.content_b64 !== 'string' || !response.content_b64) {
      throw new Error('Image fetch failed');
    }
    const source = `data:${mime};base64,${response.content_b64}`;
    img.src = source;
    await img.decode();
    if (!img.isConnected) return;
    button.dataset.viewerSrc = source;
    status?.remove();
  } catch {
    if (!img.isConnected) return;
    button.classList.add('is-error');
    if (status) status.textContent = 'Image unavailable';
  }
}

function hydrateImageAttachments(root, fetchBlob) {
  for (const img of root.querySelectorAll('.slot-chat-media-button img[data-needs-blob="1"]')) {
    void hydrateImage(img, fetchBlob);
  }
}

module.exports = { hydrateImageAttachments };
