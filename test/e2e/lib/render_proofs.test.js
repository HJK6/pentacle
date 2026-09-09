const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {
  expectedQuestionOptionLabels,
  renderedQuestionOptionLabels,
  compareQuestionOptionLabels,
  buildQuestionLabelProofScript,
  countTranscriptRows,
} = require('./render_proofs');

function optionButton({ questionIndex = null, optionIndex, label }) {
  const attrs = { 'data-option': String(optionIndex) };
  if (questionIndex !== null) attrs['data-question-index'] = String(questionIndex);
  return {
    textContent: label,
    getAttribute(name) {
      return attrs[name] ?? null;
    },
    querySelector(selector) {
      return selector === '.slot-chat-question-option-label' ? { textContent: label } : null;
    },
    closest(selector) {
      if (selector === '.slot-chat-question-card[data-question-index]' && questionIndex !== null) {
        return { getAttribute: (name) => (name === 'data-question-index' ? String(questionIndex) : null) };
      }
      return null;
    },
  };
}

function questionRoot(buttons) {
  return {
    querySelectorAll(selector) {
      return selector === '.slot-chat-question-option[data-option]' ? buttons : [];
    },
  };
}

function cellRoot({ slotChatChrome = 0, transcriptRows = 0 } = {}) {
  return {
    querySelectorAll(selector) {
      if (selector === '.slot-chat-row') return Array.from({ length: transcriptRows }, () => ({}));
      if (selector === '[class*=slot-chat]') return Array.from({ length: slotChatChrome + transcriptRows }, () => ({}));
      return [];
    },
  };
}

function oldQuestionProof(question, root) {
  const expected = expectedQuestionOptionLabels(question);
  const rendered = renderedQuestionOptionLabels(root);
  return rendered.length === expected.length
    && expected.every((row) => rendered.some((actual) => actual.optionIndex === row.optionIndex));
}

test('question label proof fails when rendered labels are pane noise despite matching counts and indices', () => {
  const question = {
    options: [
      { index: 1, label: 'One' },
      { index: 2, label: 'Two' },
      { index: 3, label: 'Three' },
    ],
  };
  const root = questionRoot([
    optionButton({ optionIndex: 1, label: 'connected' }),
    optionButton({ optionIndex: 2, label: '\u2500\u2500' }),
    optionButton({ optionIndex: 3, label: '[32mThree' }),
  ]);

  assert.equal(oldQuestionProof(question, root), true);
  const proof = compareQuestionOptionLabels(
    expectedQuestionOptionLabels(question),
    renderedQuestionOptionLabels(root),
  );
  assert.equal(proof.ok, false);
  assert.equal(proof.rendered.length, 3);
  assert.ok(proof.noiseLabels.length >= 2);
  assert.ok(proof.mismatches.length >= 2);
});

test('question label proof passes when rendered data-option labels match parsed labels', () => {
  const question = {
    questions: [
      { index: 0, options: [{ index: 1, label: 'Red' }, { index: 2, label: 'Blue' }] },
      { index: 1, options: [{ index: 1, label: 'Small' }, { index: 2, label: 'Large' }] },
    ],
  };
  const root = questionRoot([
    optionButton({ questionIndex: 0, optionIndex: 1, label: 'Red' }),
    optionButton({ questionIndex: 0, optionIndex: 2, label: 'Blue' }),
    optionButton({ questionIndex: 1, optionIndex: 1, label: 'Small' }),
    optionButton({ questionIndex: 1, optionIndex: 2, label: 'Large' }),
  ]);
  const proof = compareQuestionOptionLabels(
    expectedQuestionOptionLabels(question),
    renderedQuestionOptionLabels(root),
  );
  assert.equal(proof.ok, true);
});

test('browser question proof script compares live getQuestion payload to rendered labels', () => {
  const root = questionRoot([
    optionButton({ optionIndex: 1, label: 'Alpha' }),
    optionButton({ optionIndex: 2, label: 'Beta' }),
  ]);
  const context = {
    document: { querySelector: (selector) => (selector === '#cell-0' ? root : null) },
    window: {
      PentacleChatStore: {
        getQuestion() {
          return { options: [{ index: 1, label: 'Alpha' }, { index: 2, label: 'Beta' }] };
        },
      },
    },
  };

  const proof = JSON.parse(vm.runInNewContext(buildQuestionLabelProofScript('#cell-0', JSON.stringify('stream-1')), context));
  assert.equal(proof.ok, true);
});

test('transcript row proof fails when slot-chat chrome exists but transcript rows do not', () => {
  const cell = cellRoot({ slotChatChrome: 3, transcriptRows: 0 });

  assert.equal(cell.querySelectorAll('[class*=slot-chat]').length > 0, true);
  assert.equal(countTranscriptRows(cell) > 0, false);
});

test('transcript row proof counts conversational row wrappers only', () => {
  assert.equal(countTranscriptRows(cellRoot({ slotChatChrome: 3, transcriptRows: 2 })), 2);
});
