const test = require('node:test');
const assert = require('node:assert/strict');

const titleHelpers = require('../main/title_helpers');

test('cleanTitleCandidate accepts short manual-style titles', () => {
  assert.equal(
    titleHelpers.cleanTitleCandidate(' "manual rename flow" '),
    'Manual Rename Flow',
  );
});

test('cleanTitleCandidate rejects invalid title candidates', () => {
  assert.equal(titleHelpers.cleanTitleCandidate('Single'), '');
  assert.equal(titleHelpers.cleanTitleCandidate('Title With - Dash'), '');
  assert.equal(titleHelpers.cleanTitleCandidate('codex'), '');
});
