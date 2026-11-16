'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { validateVoiceAnswers } = require('../main/voice_answers_meta');

const directory = path.join(__dirname, 'fixtures/voice_answers_meta');
for (const file of fs.readdirSync(directory).filter(name => name.endsWith('.json')).sort()) {
  for (const fixture of JSON.parse(fs.readFileSync(path.join(directory, file), 'utf8'))) {
    test(`${file}: ${fixture.name}`, () => {
      const before = structuredClone(fixture.input);
      assert.deepEqual(validateVoiceAnswers(fixture.input), fixture.expected);
      assert.deepEqual(fixture.input, before, 'normalization never mutates the binding');
    });
  }
}

const valid = JSON.parse(fs.readFileSync(path.join(directory, 'accept.json'), 'utf8'))[0].input;
for (const field of ['duration_s', 'start_s', 'end_s']) {
  for (const value of [NaN, Infinity, -Infinity, undefined]) {
    test(`${field} rejects ${String(value)}`, () => {
      const raw = structuredClone(valid);
      (field === 'duration_s' ? raw : raw.items[0].segment)[field] = value;
      assert.equal(validateVoiceAnswers(raw), null);
    });
  }
}

test('Python 3dp rounding includes exact half-even ties and large finite seconds', () => {
  const vectors = [[1.2345, 1.234], [1.2355, 1.236], [.0625, .062], [.1875, .188],
    [4398046511104.0625, 4398046511104.062], [1e100, 1e100], [Number.MIN_VALUE, 0]];
  for (const [input, expected] of vectors) {
    assert.equal(validateVoiceAnswers({ ...valid, duration_s: input }).duration_s, expected);
  }
});

test('a fresh closed binding drops every extra key and nested alias', () => {
  const raw = structuredClone(valid);
  raw.admin = true; raw.items[0].stale = true; raw.items[0].segment.inject = true;
  const result = validateVoiceAnswers(raw);
  assert.deepEqual(result, valid);
  result.items[0].segment.end_s = 99;
  assert.equal(raw.items[0].segment.end_s, 3.2);
});

test('21 unique-item vector isolates the cap rather than duplicate-key rejection', () => {
  const fixture = JSON.parse(fs.readFileSync(path.join(directory, 'reject_items.json'), 'utf8'))
    .find(item => item.name === '21 unique valid items exceed the 20-item cap');
  assert.equal(new Set(fixture.input.items.map(item => item.key)).size, 21);
  assert.equal(validateVoiceAnswers({ ...fixture.input, items: fixture.input.items.slice(0, 20) }).items.length, 20);
  assert.equal(validateVoiceAnswers(fixture.input), null);
});
