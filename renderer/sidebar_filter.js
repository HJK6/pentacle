function offlineHostStatus(summary) {
  const row = summary || {};
  if (row.host_status !== 'offline' && row.host_status_reason !== 'unreachable') return null;
  const since = typeof row.host_status_since === 'string' && row.host_status_since.trim();
  return since ? `Offline since ${since}` : 'Offline';
}

// Sidebar rows come only from chat_streamd and fail closed on visibility.
function filterSidebarSessions(sessions) {
  return (sessions || []).filter((session) => session?.visibility === 'default');
}

// collectSourceFilterHostIds returns the unique hostIds of the visible-session
// list, which feeds the sidebar's source-filter button row. It MUST be called
// with the post-filterSidebarSessions list — never raw state.sessions —
// so a host whose only sessions are nested QA subagents does not produce a
// filter button.
function collectSourceFilterHostIds(visibleSessions) {
  const seen = new Set();
  const out = [];
  for (const session of visibleSessions || []) {
    const hostId = session.hostId;
    if (!hostId || seen.has(hostId)) continue;
    seen.add(hostId);
    out.push(hostId);
  }
  return out;
}

// nextChatStreamSessions returns what state.chatStream.sessions should become
// given the current cache and an incoming chat-stream payload. Status-only
// payloads preserve the cache. An explicit daemon inventory is a replacement,
// never a membership merge hint: absent managed rows disappear at that
// revision.
function nextChatStreamSessions(currentSessions, payload, options = {}) {
  const previous = Array.isArray(currentSessions) ? currentSessions : [];
  if (!payload || !Array.isArray(payload.sessions)) return previous;
  if (options.spawnFailureNotifications && options.onSpawnFailure) {
    for (const session of payload.sessions) {
      if ((session?.state || session?.bootstrap_state) !== 'failed' || !session.reason) continue;
      const streamId = String(session.stream_id || '');
      const generation = String(session.session_generation || '');
      const notificationKey = `${streamId}\0${generation}`;
      if (!streamId || options.spawnFailureNotifications.has(notificationKey)) continue;
      options.spawnFailureNotifications.add(notificationKey);
      options.onSpawnFailure(session);
    }
  }
  return payload.sessions;
}

// projectChatStreamSessionsToDesktop maps the chat-stream daemon's session
// summaries into the shape state.sessions expects, so the sidebar can
// render the moment the WS hello snapshot arrives — without waiting for
// fetchSessions's per-host tmux fan-out (which is bottlenecked by the
// slowest SSH).
//
// `hostIdResolver(streamHost)` is injected because the renderer-side
// reverse mapping depends on the configured HOST_IDS list, which only
// app.js has visibility into. Callers should pass a function that returns
// the desktop hostId for a chat-stream host, or null/undefined if unknown.
function projectChatStreamSessionsToDesktop(streamSessions, hostIdResolver) {
  const out = [];
  for (const s of streamSessions || []) {
    if (!s) continue;
    const name = s.session_name || s.name;
    if (!name) continue;
    const resolvedHostId = typeof hostIdResolver === 'function' ? hostIdResolver(s.host) : null;
    out.push({
      name,
      session_name: name,
      hostId: resolvedHostId || s.hostId || null,
      host: s.host,
      display_name: s.display_name || s.title || name,
      title: s.title || '',
      preview: s.preview || s.last_text || '',
      attached: !!s.attached,
      agent_id: s.agent_id || null,
      stream_id: s.stream_id || null,
      visibility: s.visibility,
      provider: s.provider || null,
      working: !!s.working,
      working_label: s.working_label || '',
      last_event_at: s.last_event_at || null,
      last_text: s.last_text || '',
      last_kind: s.last_kind || null,
      online: s.online !== false,
      pane_pid: s.pane_pid || null,
      created: s.created || 0,
      last_activity: s.last_activity || null,
    });
  }
  return out;
}

module.exports = {
  offlineHostStatus,
  filterSidebarSessions,
  collectSourceFilterHostIds,
  nextChatStreamSessions,
  projectChatStreamSessionsToDesktop,
};
