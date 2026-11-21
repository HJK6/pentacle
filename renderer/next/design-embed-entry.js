'use strict';

const { createDesignViewer } = require('./design_viewer');
const { initialDesignRoute, replaceDesignAddress } = require('./design_location');

document.documentElement.dataset.theme = 'cosmic';
const route = initialDesignRoute(window, 'embed');
if (route) {
  const root = document.getElementById('app');
  const viewer = createDesignViewer(root, {
    embedded: true,
    onNavigate: target => replaceDesignAddress(window, target, 'embed'),
  });
  document.addEventListener('keydown', event => {
    if (viewer.handleKey(event)) event.preventDefault();
  });
  window.addEventListener('pagehide', event => {
    if (!event.persisted) viewer.close();
  });
  void viewer.open(route.target);
}
