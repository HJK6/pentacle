(function() {

function _fmtTs(ts) {
  if (!ts) return '';
  try { return new Date(ts).toLocaleTimeString(); } catch (_) { return ''; }
}

function _escape(text) {
  return String(text || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
}

function _kindColor(kind) {
  if (kind === 'USER') return '#79c0ff';
  if (kind === 'ASSIST') return '#56d364';
  if (kind === 'TOOL' || kind === 'TOOL-OUT') return '#d4a72c';
  if (kind === 'THINK') return '#b8a0fa';
  return '#9aa4af';
}

function _hostColor(host) {
  if (host === 'bart') return '#a78bfa';
  if (host === 'merlin') return '#d4a72c';
  if (host === 'amaterasu') return '#f47067';
  return '#2dd4bf';
}

function _groupSessions(events) {
  const sessions = new Map();
  for (const event of events || []) {
    const key = event.stream_id || `${event.host}:${event.provider}:${event.session_id || 'unknown'}`;
    if (!sessions.has(key)) {
      sessions.set(key, {
        key,
        host: event.host || 'unknown',
        provider: event.provider || 'unknown',
        sessionId: event.session_id || '',
        lastTimestamp: event.timestamp || '',
        lastText: event.text || '',
        lastKind: event.kind || 'EVENT',
        events: [],
      });
    }
    const session = sessions.get(key);
    session.events.push(event);
    session.lastTimestamp = event.timestamp || session.lastTimestamp;
    session.lastText = event.text || session.lastText;
    session.lastKind = event.kind || session.lastKind;
  }
  return Array.from(sessions.values()).sort((a, b) => String(b.lastTimestamp).localeCompare(String(a.lastTimestamp)));
}

function _renderSessionList(sessions) {
  if (!sessions.length) {
    return '<div style="padding:18px;border:1px dashed #2a3b33;border-radius:10px;color:#7f9187;">No chat-stream sessions yet.</div>';
  }
  return sessions.map((session) => `
    <div data-stream-id="${_escape(session.key)}" style="border:1px solid #24342d;border-radius:12px;padding:12px;background:#121a16;cursor:pointer;">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px;">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
          <span style="font-size:11px;padding:3px 7px;border-radius:999px;background:${_hostColor(session.host)}22;color:${_hostColor(session.host)};text-transform:uppercase;">${_escape(session.host)}</span>
          <span style="font-size:11px;padding:3px 7px;border-radius:999px;background:#173126;color:#8ee4bf;text-transform:uppercase;">${_escape(session.provider)}</span>
        </div>
        <span style="font-size:11px;color:#93a39a;">${_fmtTs(session.lastTimestamp)}</span>
      </div>
      <div style="font-size:12px;color:#6f8478;margin-bottom:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${_escape(session.sessionId || session.key)}</div>
      <div style="font-size:13px;line-height:1.4;color:#e6eee9;">${_escape(session.lastText || '')}</div>
    </div>`).join('');
}

function _renderTimeline(events) {
  if (!events.length) {
    return '<div style="padding:18px;border:1px dashed #2a3b33;border-radius:10px;color:#7f9187;">Select a session to inspect its transcript.</div>';
  }
  return events.slice(-120).reverse().map((event) => `
    <div style="border:1px solid #222f29;border-radius:10px;padding:10px 12px;background:#151d19;">
      <div style="display:flex;gap:10px;align-items:center;margin-bottom:6px;flex-wrap:wrap;">
        <span style="font-size:11px;color:#93a39a;">${_fmtTs(event.timestamp)}</span>
        <span style="font-size:11px;color:${_kindColor(event.kind)};text-transform:uppercase;">${_escape(event.kind || 'EVENT')}</span>
      </div>
      <div style="white-space:pre-wrap;line-height:1.45;color:#edf3ef;">${_escape(event.text || '')}</div>
    </div>`).join('');
}

function mount(container) {
  container.innerHTML = '';
  const root = document.createElement('div');
  root.style.cssText = 'padding:16px;font-family:-apple-system,Helvetica,Arial,sans-serif;font-size:13px;color:#d7e4dc;height:100%;display:flex;flex-direction:column;gap:14px;background:linear-gradient(180deg,#0f1713,#0d1310);';

  const header = document.createElement('div');
  header.style.cssText = 'display:flex;justify-content:space-between;align-items:flex-start;gap:16px;';
  header.innerHTML = `
    <div>
      <h2 style="margin:0 0 4px 0;font-size:20px;font-weight:700;color:#f2fbf5;">Agent Stream</h2>
      <div style="font-size:13px;color:#8aa097;">Merged Claude and Codex transcript stream across Bart and Merlin.</div>
    </div>
    <div data-role="status" style="font-size:12px;color:#8aa097;padding-top:4px;"></div>`;
  root.appendChild(header);

  const stats = document.createElement('div');
  stats.style.cssText = 'display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;';
  root.appendChild(stats);

  const layout = document.createElement('div');
  layout.style.cssText = 'display:grid;grid-template-columns:minmax(300px,360px) minmax(0,1fr);gap:14px;min-height:0;flex:1;';
  layout.innerHTML = `
    <div style="display:flex;flex-direction:column;min-height:0;">
      <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.6px;color:#6f8478;margin:0 0 8px 0;">Live Sessions</div>
      <div data-role="sessions" style="display:flex;flex-direction:column;gap:10px;overflow:auto;min-height:0;padding-right:4px;"></div>
    </div>
    <div style="display:flex;flex-direction:column;min-height:0;">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;margin:0 0 8px 0;">
        <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.6px;color:#6f8478;">Transcript</div>
        <div data-role="selected-label" style="font-size:12px;color:#93a39a;"></div>
      </div>
      <div data-role="timeline" style="display:flex;flex-direction:column;gap:8px;overflow:auto;min-height:0;padding-right:4px;"></div>
    </div>`;
  root.appendChild(layout);

  container.appendChild(root);
  return {
    status: header.querySelector('[data-role="status"]'),
    stats,
    sessions: layout.querySelector('[data-role="sessions"]'),
    timeline: layout.querySelector('[data-role="timeline"]'),
    selectedLabel: layout.querySelector('[data-role="selected-label"]'),
    selectedStreamId: null,
    lastSnapshot: null,
  };
}

function _renderStats(refs, sessions, events, connected) {
  const claudeCount = sessions.filter((s) => s.provider === 'claude').length;
  const codexCount = sessions.filter((s) => s.provider === 'codex').length;
  const hosts = new Set(sessions.map((s) => s.host)).size;
  const cards = [
    { label: 'Connection', value: connected ? 'Live' : 'Offline', color: connected ? '#56d364' : '#f47067' },
    { label: 'Hosts', value: String(hosts), color: '#79c0ff' },
    { label: 'Claude', value: String(claudeCount), color: '#a78bfa' },
    { label: 'Codex', value: String(codexCount), color: '#2dd4bf' },
  ];
  refs.stats.innerHTML = cards.map((card) => `
    <div style="border:1px solid #23342c;border-radius:12px;padding:12px;background:#131c17;">
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:#7d9488;">${_escape(card.label)}</div>
      <div style="margin-top:6px;font-size:22px;font-weight:700;color:${card.color};">${_escape(card.value)}</div>
    </div>`).join('');
}

function update(refs, data) {
  if (!refs || !data) return;
  refs.lastSnapshot = data;
  const events = Array.isArray(data.events) ? data.events : [];
  const sessions = _groupSessions(events);

  if (!refs.selectedStreamId && sessions.length) {
    refs.selectedStreamId = sessions[0].key;
  }
  if (refs.selectedStreamId && !sessions.some((s) => s.key === refs.selectedStreamId)) {
    refs.selectedStreamId = sessions[0] ? sessions[0].key : null;
  }

  refs.status.textContent = data.connected ? 'Websocket connected' : 'Disconnected, showing cached state';
  refs.status.style.color = data.connected ? '#56d364' : '#f47067';
  _renderStats(refs, sessions, events, !!data.connected);

  refs.sessions.innerHTML = _renderSessionList(sessions);
  refs.sessions.querySelectorAll('[data-stream-id]').forEach((el) => {
    const active = el.getAttribute('data-stream-id') === refs.selectedStreamId;
    if (active) {
      el.style.borderColor = '#2dd4bf';
      el.style.background = '#15221d';
    }
    el.addEventListener('click', () => {
      refs.selectedStreamId = el.getAttribute('data-stream-id');
      update(refs, refs.lastSnapshot);
    });
  });

  const selected = sessions.find((s) => s.key === refs.selectedStreamId) || null;
  refs.selectedLabel.textContent = selected ? `${selected.host} · ${selected.provider}` : '';
  refs.timeline.innerHTML = _renderTimeline(selected ? selected.events : []);
}

function unmount(_refs) {}

window.DASHBOARDS.push({
  id: 'chat-stream',
  name: 'Agent Stream',
  description: 'Merged Claude and Codex event stream from Bart and Merlin',
  color: 'var(--cyan, #2dd4bf)',
  mount,
  update,
  unmount,
  pollFn: () => window.cc.getChatStreamState(),
  pollInterval: 1500,
  idlePollInterval: 4000,
  idleFn: (data) => !data || !data.connected,
});

})();
