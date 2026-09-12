'use strict';

function configuredAssistantRole(config = {}) {
  const value = config?.features?.assistantRole;
  return typeof value === 'string' && value.trim() ? value : '';
}

function streamHostForHostId(config = {}, hostId) {
  const mapped = config?.chatStream?.hostMap?.[hostId];
  const value = mapped || (hostId === 'local' && (config?.localHostId || config?.chatStream?.localHost)) || hostId;
  return typeof value === 'string' ? value.trim() : '';
}

function sessionIdentifiers(session = {}) {
  return [session.stream_id, session.session_name, session.session_id, session.name]
    .filter((value) => typeof value === 'string' && value.trim());
}

function isProtectedAssistantRename(config, snapshot, hostId, sessionName) {
  const role = configuredAssistantRole(config);
  const name = typeof sessionName === 'string' ? sessionName.trim() : '';
  if (!role || !name || !Array.isArray(snapshot?.sessions)) return false;
  const expectedHost = streamHostForHostId(config, hostId);
  const requestedHost = typeof hostId === 'string' ? hostId.trim() : '';
  return snapshot.sessions.some((session) => {
    if (!session || session.role !== role || !sessionIdentifiers(session).includes(name)) return false;
    const host = typeof session.host === 'string' ? session.host.trim() : '';
    return !!host && (host === expectedHost || host === requestedHost);
  });
}

module.exports = { configuredAssistantRole, isProtectedAssistantRename };
