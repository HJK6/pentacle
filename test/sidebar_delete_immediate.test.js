const test = require('node:test');
const assert = require('node:assert/strict');

const { filterSidebarSessions, nextChatStreamSessions } = require('../renderer/sidebar_filter');

function visibleNames(sessions) {
  return filterSidebarSessions(sessions).map((session) => session.session_name || session.name);
}

test('sidebar removes a closed row when the next daemon inventory omits it', () => {
  const before = [
    { host: 'hosta', session_name: 'claude-hosta-keep', visibility: 'default' },
    { host: 'hosta', session_name: 'claude-hosta-delete', visibility: 'default' },
  ];
  const after = nextChatStreamSessions(before, {
    sessions: [{ host: 'hosta', session_name: 'claude-hosta-keep', visibility: 'default' }],
  });

  assert.deepEqual(visibleNames(before), ['claude-hosta-keep', 'claude-hosta-delete']);
  assert.deepEqual(visibleNames(after), ['claude-hosta-keep']);
});

