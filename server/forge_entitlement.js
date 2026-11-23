'use strict';

function createForgeEntitlement(operatorLogin) {
  const viewers = new WeakMap();
  const minted = new WeakSet();
  function viewerFor(req, auth) {
    if (!viewers.has(req)) {
      const login = auth?.verifiedLogin?.(req);
      const viewer = login ? Object.freeze({ login }) : null;
      if (viewer) minted.add(viewer);
      viewers.set(req, viewer);
    }
    return viewers.get(req);
  }
  function canReadForge(verifiedViewer) {
    return Boolean(operatorLogin && minted.has(verifiedViewer) && verifiedViewer.login === operatorLogin);
  }
  return { viewerFor, canReadForge };
}

module.exports = { createForgeEntitlement };
