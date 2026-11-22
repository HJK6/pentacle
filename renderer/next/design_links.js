'use strict';

function markDesignLinks(transcript, path) {
  for (const link of transcript.querySelectorAll('[data-next-design-link]')) {
    link.setAttribute('aria-pressed', String(path === link.dataset.nextDesignLink));
  }
}

function hydrateDesignPreviews(transcript, fetchBlob) {
  for (const img of transcript.querySelectorAll('.next-design-preview-image img')) {
    const well = img.parentElement;
    const show = () => { well.hidden = false; };
    const loadAttachment = async () => {
      if (!img.dataset.png) return;
      try {
        const result = await fetchBlob(img.dataset.png);
        if (!img.isConnected || !result?.ok || !result.content_b64) return;
        img.removeAttribute('src');
        img.addEventListener('load', show, { once: true });
        img.src = 'data:image/png;base64,' + result.content_b64;
      } catch {
        // The button remains a text preview if neither source can be read.
      }
    };
    if (img.dataset.media) {
      img.addEventListener('load', show, { once: true });
      img.addEventListener('error', loadAttachment, { once: true });
      img.src = img.dataset.media;
    } else {
      void loadAttachment();
    }
  }
}

module.exports = { markDesignLinks, hydrateDesignPreviews };
