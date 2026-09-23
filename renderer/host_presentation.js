'use strict';

const PALETTE = ['forest-green', 'red', 'royal-blue', 'mauve', 'yellow'];
const ACCENTS = { 'forest-green': '#1fbf4a', 'royal-blue': '#1f5bff', red: '#ff2e3e', orange: '#a377a1', mauve: '#a377a1', yellow: '#ffd60a', green: 'var(--green)', blue: 'var(--blue)', purple: 'var(--purple)', cyan: 'var(--cyan)' };
const text = value => typeof value === 'string' ? value.trim() : '';

function localIdentity(config = {}) {
  return text(config.localHostId) || text(config.chatStream?.hostMap?.local) || text(config.chatStream?.localHost) || 'local';
}

function streamHost(config = {}, hostId) {
  const id = text(hostId) || 'local';
  return text(config.chatStream?.hostMap?.[id]) || (id === 'local' ? text(config.chatStream?.localHost) || 'local' : id);
}

function hostLabel(config = {}, hostId) {
  const id = text(hostId) || 'local';
  const identity = id === 'local' ? localIdentity(config) : streamHost(config, id);
  const name = text(config.hostNames?.[id]) || text(config.hostNames?.[identity]);
  if (name) return name;
  const [first = '', ...rest] = Array.from(identity);
  return first.toUpperCase() + rest.join('');
}

function hostColor(config = {}, hostId, roster = config.chatStream?.hosts || ['local']) {
  const id = text(hostId) || 'local';
  const identity = id === 'local' ? localIdentity(config) : streamHost(config, id);
  const configured = text(config.hostColors?.[id]) || text(config.hostColors?.[identity]);
  if (configured && Object.hasOwn(ACCENTS, configured)) return configured;
  const ids = Array.isArray(roster) ? roster : ['local'];
  let index = ids.indexOf(id);
  if (index < 0) index = ids.findIndex(entry => streamHost(config, entry) === identity);
  return PALETTE[Math.max(0, index) % PALETTE.length];
}

// Decorative identity follows the palette, never an operator's display label.
function hostSigil(config = {}, hostId, roster = config.chatStream?.hosts || ['local']) {
  const color = hostColor(config, hostId, roster);
  return ({ 'forest-green': 'djinni', green: 'djinni', 'royal-blue': 'mage', blue: 'mage', cyan: 'mage', red: 'sun', orange: 'flower', mauve: 'flower', purple: 'flower', yellow: 'ibis' })[color];
}

function initial(label) { return (Array.from(text(label))[0] || '').toUpperCase(); }

module.exports = { PALETTE, ACCENTS, localIdentity, streamHost, hostLabel, hostColor, hostSigil, initial };
