'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
  configuredAssistantRole,
  isConfiguredAssistant,
} = require('../renderer/assistant_role');

test('assistant protection is opt-in and exact-role only', () => {
  assert.equal(configuredAssistantRole(require('../pentacle.config.example').features), '');
  assert.equal(configuredAssistantRole({}), '');
  assert.equal(configuredAssistantRole({ assistantRole: '' }), '');
  assert.equal(configuredAssistantRole({ assistantRole: 'persistent-assistant' }), 'persistent-assistant');
  assert.equal(isConfiguredAssistant({ role: 'persistent-assistant' }, 'persistent-assistant'), true);
  assert.equal(isConfiguredAssistant({ role: 'Persistent-Assistant' }, 'persistent-assistant'), false);
  assert.equal(isConfiguredAssistant({ role: 'persistent-assistant' }, ''), false);
  assert.equal(isConfiguredAssistant({ title: 'persistent-assistant' }, 'persistent-assistant'), false);
});
