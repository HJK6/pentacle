'use strict';

// A private desktop overlay opts in by configuring the exact daemon role that
// represents its persistent assistant. This deliberately has no display-name,
// host, or title fallback: public/default configurations remain inert.
function configuredAssistantRole(features) {
  const role = features && features.assistantRole;
  return typeof role === 'string' && role.length > 0 ? role : '';
}

function isConfiguredAssistant(session, assistantRole) {
  return !!assistantRole && !!session && session.role === assistantRole;
}

module.exports = { configuredAssistantRole, isConfiguredAssistant };
