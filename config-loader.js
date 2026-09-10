'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

function machineKey(hostname = os.hostname()) {
  return String(hostname || '').toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || 'local';
}

function candidateConfigPaths(root = __dirname, env = process.env) {
  const base = path.resolve(root);
  // A named private overlay is authoritative. A typo must be visible instead
  // of silently starting with a different topology.
  if (env.PENTACLE_CONFIG) return [path.resolve(env.PENTACLE_CONFIG)];
  return [path.join(base, 'pentacle.config.js'), path.join(base, 'pentacle.config.example.js')];
}

// Theme blocks (dark/light/terminal) are read unconditionally at startup —
// e.g. main.js BrowserWindow `backgroundColor: CONFIG.dark.bg`. A config that
// omits any of them crashes the app at launch with "Cannot read properties of
// undefined (reading 'bg')" and no usable window appears. This is especially a
// trap for the PACKAGED app, whose resolved config can differ from the dev
// `pentacle.config.js`. So we always backfill the three theme blocks from the
// canonical example config (a bundled candidate), with an inline minimal
// fallback if the example can't be loaded. Pentacle is dark-only; there is no
// light theme. Pure presentation — no logic change.
const THEME_KEYS = ['dark', 'terminal'];
const FALLBACK_THEMES = {
  dark: { bg: '#0c1310', bg2: '#121e18', bg3: '#1a2b22', fg: '#b5ccba', fgDim: '#4d6e56', border: '#1e3928', cyan: '#2dd4bf' },
  terminal: { bg: '#0c1310', fg: '#b5ccba', cursor: '#2dd4bf' },
};

function withThemeDefaults(config, root) {
  if (!config || typeof config !== 'object') return config;
  if (THEME_KEYS.every((k) => config[k] && typeof config[k] === 'object')) return config;
  let example = null;
  try {
    example = require(path.join(path.resolve(root), 'pentacle.config.example.js'));
  } catch {
    example = null;
  }
  for (const k of THEME_KEYS) {
    if (!config[k] || typeof config[k] !== 'object') {
      config[k] = (example && example[k] && typeof example[k] === 'object') ? example[k] : FALLBACK_THEMES[k];
    }
  }
  return config;
}

const TOP_LEVEL_KEYS = new Set([
  'appName', 'appId', 'agents', 'dark', 'terminal', 'features', 'chatStream',
  'hosts', 'remote', 'tmux', 'hostNames', 'hostColors', 'localHostId', 'mic',
  'micServerUrl', 'wakeWord', 'machineStats', 'artifactDirs', 'repoRoots',
  'dashboardHub', 'localSsh', 'localWsl', 'localTmux', 'peers',
]);
const loggedWarnings = new Set();
const isMap = value => value && typeof value === 'object' && !Array.isArray(value);
const nonblank = value => typeof value === 'string' && value.trim().length > 0;

function configWarnings(config = {}) {
  const warnings = [];
  if (Array.isArray(config.chatStream?.hosts) && config.chatStream.hosts.length > 1) {
    const missing = ['hostNames', 'hostColors'].filter(key => !isMap(config[key]));
    if (missing.length) warnings.push({ code: 'multi-host-presentation', message: `Multiple hosts configured without ${missing.join(' and ')}. Using capitalised names and the indexed colour palette; configure these maps to customise them.` });
  }
  if (config.features?.mic === true && !nonblank(config.micServerUrl)
      && !(config.mic?.useStreamHost === true && nonblank(config.chatStream?.url))) {
    warnings.push({ code: 'mic-endpoint', message: 'Microphone enabled without an explicit endpoint. Set micServerUrl or mic.useStreamHost with chatStream.url; the fallback is http://127.0.0.1:7780.' });
  }
  const unknown = Object.keys(config).filter(key => !TOP_LEVEL_KEYS.has(key)).sort();
  if (unknown.length) warnings.push({ code: 'unknown-top-level', message: `Unknown desktop config keys: ${unknown.map(key => JSON.stringify(key)).join(', ')}. Check docs/desktop_config.md for supported keys.` });
  return warnings;
}

function loadConfig(root = __dirname, env = process.env, hostname = os.hostname()) {
  const tried = [];
  for (const file of candidateConfigPaths(root, env, hostname)) {
    tried.push(file);
    if (!fs.existsSync(file)) continue;
    const config = withThemeDefaults(require(file), root);
    const warnings = configWarnings(config);
    if (process.type !== 'renderer') {
      for (const warning of warnings) {
        const key = `${file}:${warning.code}`;
        if (loggedWarnings.has(key)) continue;
        loggedWarnings.add(key);
        console.warn(`[desktop-config:${warning.code}] ${warning.message}`);
      }
    }
    return { config, warnings, path: file, tried };
  }
  throw new Error(`No Pentacle config found. Tried: ${tried.join(', ')}`);
}

module.exports = { loadConfig, machineKey, candidateConfigPaths, withThemeDefaults, configWarnings };
