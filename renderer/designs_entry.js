'use strict';
const library = require('../vendor/dashboard-library/dist/dashboard-library.js');
const refs = library.mountGallery(document.getElementById('designs'));
let disposed = false, controller;
async function refresh() {
  if (disposed || controller) return;
  controller = new AbortController();
  try {
    const response = await fetch('/api/designs', { signal: controller.signal });
    if (!response.ok) throw new Error(response.status === 401 ? 'Authentication required. Reload to sign in.' : 'Library unavailable. Refresh to retry.');
    library.updateGallery(refs, await response.json());
  } catch (error) { if (!disposed && error.name !== 'AbortError') library.updateGallery(refs, { error: error.message }); }
  finally { controller = null; }
}
const timer = setInterval(refresh, 5000);
window.addEventListener('pagehide', () => { disposed = true; clearInterval(timer); controller?.abort(); library.unmountGallery(refs); }, { once: true });
refresh();
