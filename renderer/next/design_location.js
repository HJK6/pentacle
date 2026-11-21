'use strict';

const { parseDesignRoute } = require('../../lib/design_routes.cjs');
const { buildDesignPath } = require('../../pentacle-chat-core/src/services/dashboardLinks.ts');

function initialDesignRoute(window, presentation) {
  const path = window.location.pathname + window.location.search;
  const route = parseDesignRoute(path);
  if (route?.mode !== presentation) return null;
  if (window.location.hash) window.history.replaceState(window.history.state, '', path);
  return route;
}

function replaceDesignAddress(window, target, presentation) {
  const path = buildDesignPath(target ?? undefined, presentation);
  if (path && path !== window.location.pathname) {
    window.history.replaceState(window.history.state, '', path);
  }
}

module.exports = { initialDesignRoute, replaceDesignAddress };
