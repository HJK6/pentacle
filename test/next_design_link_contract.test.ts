import assert from 'node:assert/strict';
import test from 'node:test';
import { buildDesignPath, parseDashboardLinkEvent } from 'pentacle-chat-core';
import fixture from '../pentacle-chat-core/tests/fixtures/dashboard_links_v1.json';

const { parseDesignRoute } = require('../lib/design_routes.cjs');

test('web Design routes round-trip the released shared contract in both presentations', () => {
  for (const item of fixture.event_cases) {
    const link = parseDashboardLinkEvent(item.event);
    assert.deepEqual(link, item.link, item.name);
    if (!link) continue;
    for (const mode of ['workspace', 'embed'] as const) {
      const address = buildDesignPath(link.target, mode);
      assert.equal(address, item.paths[mode], item.name);
      assert.deepEqual(parseDesignRoute(address), { mode, target: link.target ?? null }, item.name);
    }
  }
  for (const item of fixture.rejected_paths) {
    for (const mode of ['workspace', 'embed'] as const) {
      assert.equal(parseDesignRoute(item[mode]), null, item.name);
    }
  }
});
