'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
test('synthetic assertion failure', () => assert.equal('actual', 'expected'));
