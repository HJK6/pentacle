'use strict';

// Legacy pane assistants opt in through an exact private role. A composite is
// identified by daemon metadata; neither path infers identity from a title.
function configuredAssistantRole(features) {
  const role = features && features.assistantRole;
  return typeof role === 'string' && role.length > 0 ? role : '';
}

function isConfiguredAssistant(session, assistantRole) {
  return isCompositeAssistant(session) || (!!assistantRole && !!session && session.role === assistantRole);
}

function isCompositeAssistant(session) {
  return session?.session_kind === 'assistant_composite';
}

module.exports = { configuredAssistantRole, isConfiguredAssistant, isCompositeAssistant };
