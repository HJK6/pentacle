'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

function machineKey(hostname = os.hostname()) {
  const h = String(hostname || '').toLowerCase();
  if (h.includes('bart')) return 'bartimaeus';
  if (h.includes('amaterasu')) return 'amaterasu';
  if (h.includes('merlin') || h.includes('abra')) return 'merlin';
  return h.replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || 'local';
}

function candidateConfigPaths(root = __dirname, env = process.env, hostname = os.hostname()) {
  const base = path.resolve(root);
  const candidates = [];
  if (env.PENTACLE_CONFIG) candidates.push(path.resolve(env.PENTACLE_CONFIG));
  candidates.push(path.join(base, 'configs', `${machineKey(hostname)}.js`));
  candidates.push(path.join(base, 'pentacle.config.js'));
  candidates.push(path.join(base, 'pentacle.config.example.js'));
  return candidates;
}

function loadConfig(root = __dirname, env = process.env, hostname = os.hostname()) {
  const tried = [];
  for (const file of candidateConfigPaths(root, env, hostname)) {
    tried.push(file);
    if (!fs.existsSync(file)) continue;
    return { config: require(file), path: file, tried };
  }
  throw new Error(`No Pentacle config found. Tried: ${tried.join(', ')}`);
}

module.exports = { loadConfig, machineKey, candidateConfigPaths };
