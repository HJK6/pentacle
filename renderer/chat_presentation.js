'use strict';

const { isConfiguredAssistant } = require('./assistant_role');

// Callers resolve identity in their own inventory before applying this rule.
function isProtectedAssistant(candidates, assistantRole) {
  return candidates.some(session => isConfiguredAssistant(session, assistantRole));
}

function modelLabel(model) {
  // Catalog ids carry their display name (claude-<family>-<major>[-<minor>],
  // gpt-<version>-<tier>); anything else renders raw.
  const id = String(model ?? '');
  const title = (word) => word[0].toUpperCase() + word.slice(1);
  const claude = id.match(/^claude-([a-z]+)-(\d+)(?:-(\d+))?$/);
  if (claude) return `${title(claude[1])} ${claude[2]}${claude[3] ? `.${claude[3]}` : ''}`;
  if (id === 'auto' || id === 'cursor-auto') return 'Cursor Auto';
  const gemini = id.match(/^gemini-(\d+(?:\.\d+)?)-([a-z]+(?:-[a-z]+)*)$/);
  if (gemini) return `Gemini ${gemini[1]} ${gemini[2].split('-').map(title).join(' ')}`;
  const gpt = id.match(/^gpt-(\d+(?:\.\d+)?)-([a-z]+)$/);
  return gpt ? `${gpt[1]} ${title(gpt[2])}` : model;
}

module.exports = { modelLabel, isProtectedAssistant };
