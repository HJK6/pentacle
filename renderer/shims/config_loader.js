'use strict';

// Browser stand-in for ../config-loader. The desktop reads the config file from
// disk inside the preload; the web host injects the *computed* config (the same
// object `get-config` returns) into window.__PENTACLE_CONFIG__ before the bundle
// runs, so the renderer sees byte-identical values on both transports.

function loadConfig() {
  const config = (typeof window !== 'undefined' && window.__PENTACLE_CONFIG__) || null;
  if (!config) {
    throw new Error('window.__PENTACLE_CONFIG__ is missing — the page was not served by the Pentacle web host');
  }
  return { config, path: 'window.__PENTACLE_CONFIG__', tried: [] };
}

function machineKey() {
  const config = (typeof window !== 'undefined' && window.__PENTACLE_CONFIG__) || {};
  return String(config.hostname || 'local').toLowerCase();
}

module.exports = { loadConfig, machineKey };
