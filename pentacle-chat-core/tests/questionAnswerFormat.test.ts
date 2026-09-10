import assert from 'node:assert/strict';
import test from 'node:test';

import {
  QUESTION_ANSWER_FORMAT_EXAMPLES,
  buildPentacleQuestionAnswerText,
  parsePentacleQuestionAnswerText,
  type PentacleQuestion,
} from '../src/index.ts';

function singleQuestion(overrides: Partial<PentacleQuestion> = {}): PentacleQuestion {
  return {
    header: 'Deploy',
    prompt: 'Choose a deployment target:',
    options: [
      { index: 1, label: 'host_c' },
      { index: 2, label: 'workstation' },
    ],
    ...overrides,
  };
}

test('formats a single-select answer with a 1-based question index', () => {
  assert.equal(
    buildPentacleQuestionAnswerText({
      question: singleQuestion(),
      answers: [{ selectedOptionLabel: 'host_c' }],
    }),
    QUESTION_ANSWER_FORMAT_EXAMPLES.single,
  );
});

test('formats a multiSelect answer as one bullet per selected label', () => {
  assert.equal(
    buildPentacleQuestionAnswerText({
      question: singleQuestion({
        header: 'Toppings',
        prompt: 'Which toppings should go on the pizza?',
        multiSelect: true,
        options: [
          { index: 1, label: 'Cheese' },
          { index: 2, label: 'Pepperoni' },
          { index: 3, label: 'Mushrooms' },
        ],
      }),
      answers: [{ selectedOptionLabels: ['Cheese', 'Mushrooms'] }],
    }),
    QUESTION_ANSWER_FORMAT_EXAMPLES.multiSelect,
  );
});

test('formats a multi-question answer with plural intro and per-question blocks', () => {
  assert.equal(
    buildPentacleQuestionAnswerText({
      question: {
        prompt: '',
        options: [],
        multi: true,
        questions: [
          { index: 0, header: 'Host', prompt: 'Which host should run the job?', options: [{ index: 1, label: 'host_c' }] },
          { index: 1, header: 'Priority', prompt: 'How urgent is the job?', options: [{ index: 1, label: 'Now' }] },
        ],
      },
      answers: [
        { selectedOptionLabel: 'host_c' },
        { selectedOptionLabel: 'Now' },
      ],
    }),
    QUESTION_ANSWER_FORMAT_EXAMPLES.multiQuestion,
  );
});

test('formats free-text answers verbatim including newlines', () => {
  assert.equal(
    buildPentacleQuestionAnswerText({
      question: singleQuestion({
        header: 'Use case',
        prompt: 'Describe your primary use case:',
        options: [],
      }),
      answers: [{ text: 'I need a machine\nthat can run tests.' }],
    }),
    QUESTION_ANSWER_FORMAT_EXAMPLES.freeText,
  );
});

test('formats an option answer with a note line', () => {
  assert.equal(
    buildPentacleQuestionAnswerText({
      question: singleQuestion(),
      answers: [{ selectedOptionIndex: 2, note: 'Use the orchestrator.' }],
    }),
    QUESTION_ANSWER_FORMAT_EXAMPLES.optionWithNote,
  );
});

test('formats a note-only answer without inventing an answer label', () => {
  assert.equal(
    buildPentacleQuestionAnswerText({
      question: singleQuestion({
        header: 'Decision',
        prompt: 'What decision should we record?',
        options: [],
      }),
      answers: [{ note: 'I need more context.' }],
    }),
    QUESTION_ANSWER_FORMAT_EXAMPLES.noteOnly,
  );
});

test('falls back to the first six prompt words when the header is absent', () => {
  assert.equal(
    buildPentacleQuestionAnswerText({
      question: singleQuestion({
        header: '',
        prompt: 'Which machine should handle the follow up verification pass?',
      }),
      answers: [{ selectedOptionLabel: 'host_c' }],
    }),
    `Answering your question:

Q1 (Which machine should handle the follow): host_c`,
  );
});

test('normalizes newlines in labels without escaping free text or notes', () => {
  assert.equal(
    buildPentacleQuestionAnswerText({
      question: singleQuestion({
        header: 'Deploy',
        options: [{ index: 1, label: 'host_c\nportable' }],
      }),
      answers: [{ selectedOptionIndex: 1, note: 'line one\nline two' }],
    }),
    `Answering your question:

Q1 (Deploy): host_c portable
note (Q1): line one
line two`,
  );
});

test('preserves CRLF and CR in free-text answers verbatim', () => {
  assert.equal(
    buildPentacleQuestionAnswerText({
      question: singleQuestion({
        header: 'Use case',
        prompt: 'Describe your primary use case:',
        options: [],
      }),
      answers: [{ text: 'line one\r\nline two\rline three' }],
    }),
    'Answering your question:\n\nQ1 (Use case):\nline one\r\nline two\rline three',
  );
});

test('preserves CRLF and CR in notes verbatim', () => {
  assert.equal(
    buildPentacleQuestionAnswerText({
      question: singleQuestion(),
      answers: [{ selectedOptionLabel: 'host_c', note: 'note one\r\nnote two\rnote three' }],
    }),
    'Answering your question:\n\nQ1 (Deploy): host_c\nnote (Q1): note one\r\nnote two\rnote three',
  );
});

// --- parsePentacleQuestionAnswerText: round-trip (build → parse) recovery ---

test('round-trips a single-select answer', () => {
  const text = buildPentacleQuestionAnswerText({
    question: singleQuestion(),
    answers: [{ selectedOptionLabel: 'host_c' }],
  });
  assert.deepEqual(parsePentacleQuestionAnswerText(text), {
    multiple: false,
    items: [{ header: 'Deploy', selectedLabels: ['host_c'] }],
  });
});

test('round-trips a multiSelect answer to its selected labels', () => {
  const text = buildPentacleQuestionAnswerText({
    question: singleQuestion({
      header: 'Toppings',
      prompt: 'Which toppings should go on the pizza?',
      multiSelect: true,
      options: [
        { index: 1, label: 'Cheese' },
        { index: 2, label: 'Pepperoni' },
        { index: 3, label: 'Mushrooms' },
      ],
    }),
    answers: [{ selectedOptionLabels: ['Cheese', 'Mushrooms'] }],
  });
  assert.deepEqual(parsePentacleQuestionAnswerText(text), {
    multiple: false,
    items: [{ header: 'Toppings', selectedLabels: ['Cheese', 'Mushrooms'] }],
  });
});

test('round-trips a multi-question answer with plural intro and per-question blocks', () => {
  const text = buildPentacleQuestionAnswerText({
    question: {
      prompt: '',
      options: [],
      multi: true,
      questions: [
        { index: 0, header: 'Host', prompt: 'Which host should run the job?', options: [{ index: 1, label: 'host_c' }] },
        { index: 1, header: 'Priority', prompt: 'How urgent is the job?', options: [{ index: 1, label: 'Now' }] },
      ],
    },
    answers: [{ selectedOptionLabel: 'host_c' }, { selectedOptionLabel: 'Now' }],
  });
  assert.deepEqual(parsePentacleQuestionAnswerText(text), {
    multiple: true,
    items: [
      { header: 'Host', selectedLabels: ['host_c'] },
      { header: 'Priority', selectedLabels: ['Now'] },
    ],
  });
});

test('round-trips a free-text answer including newlines', () => {
  const text = buildPentacleQuestionAnswerText({
    question: singleQuestion({ header: 'Use case', prompt: 'Describe your primary use case:', options: [] }),
    answers: [{ text: 'I need a machine\nthat can run tests.' }],
  });
  assert.deepEqual(parsePentacleQuestionAnswerText(text), {
    multiple: false,
    items: [{ header: 'Use case', text: 'I need a machine\nthat can run tests.' }],
  });
});

test('round-trips a free-text answer that contains a blank line', () => {
  const text = buildPentacleQuestionAnswerText({
    question: singleQuestion({ header: 'Use case', prompt: 'Describe your primary use case:', options: [] }),
    answers: [{ text: 'paragraph one\n\nparagraph two' }],
  });
  assert.deepEqual(parsePentacleQuestionAnswerText(text), {
    multiple: false,
    items: [{ header: 'Use case', text: 'paragraph one\n\nparagraph two' }],
  });
});

test('round-trips an option answer with a note line', () => {
  const text = buildPentacleQuestionAnswerText({
    question: singleQuestion(),
    answers: [{ selectedOptionIndex: 2, note: 'Use the orchestrator.' }],
  });
  assert.deepEqual(parsePentacleQuestionAnswerText(text), {
    multiple: false,
    items: [{ header: 'Deploy', selectedLabels: ['workstation'], note: 'Use the orchestrator.' }],
  });
});

test('round-trips a note-only answer with no chosen answer', () => {
  const text = buildPentacleQuestionAnswerText({
    question: singleQuestion({ header: 'Decision', prompt: 'What decision should we record?', options: [] }),
    answers: [{ note: 'I need more context.' }],
  });
  assert.deepEqual(parsePentacleQuestionAnswerText(text), {
    multiple: false,
    items: [{ header: 'Decision', note: 'I need more context.' }],
  });
});

test('round-trips the bare no-answer branch', () => {
  const text = buildPentacleQuestionAnswerText({
    question: singleQuestion(),
    answers: [{}],
  });
  assert.equal(text, 'Answering your question:\n\nQ1 (Deploy):');
  assert.deepEqual(parsePentacleQuestionAnswerText(text), {
    multiple: false,
    items: [{ header: 'Deploy' }],
  });
});

test('round-trips a header recovered from the prompt-word fallback', () => {
  const text = buildPentacleQuestionAnswerText({
    question: singleQuestion({ header: '', prompt: 'Which machine should handle the follow up verification pass?' }),
    answers: [{ selectedOptionLabel: 'host_c' }],
  });
  assert.deepEqual(parsePentacleQuestionAnswerText(text), {
    multiple: false,
    items: [{ header: 'Which machine should handle the follow', selectedLabels: ['host_c'] }],
  });
});

test('round-trips CRLF/CR in free text verbatim', () => {
  const text = buildPentacleQuestionAnswerText({
    question: singleQuestion({ header: 'Use case', prompt: 'Describe your primary use case:', options: [] }),
    answers: [{ text: 'line one\r\nline two\rline three' }],
  });
  assert.deepEqual(parsePentacleQuestionAnswerText(text), {
    multiple: false,
    items: [{ header: 'Use case', text: 'line one\r\nline two\rline three' }],
  });
});

test('round-trips CRLF/CR in a note verbatim', () => {
  const text = buildPentacleQuestionAnswerText({
    question: singleQuestion(),
    answers: [{ selectedOptionLabel: 'host_c', note: 'note one\r\nnote two\rnote three' }],
  });
  assert.deepEqual(parsePentacleQuestionAnswerText(text), {
    multiple: false,
    items: [{ header: 'Deploy', selectedLabels: ['host_c'], note: 'note one\r\nnote two\rnote three' }],
  });
});

test('parses the canonical fixtures into structured display models', () => {
  assert.deepEqual(parsePentacleQuestionAnswerText(QUESTION_ANSWER_FORMAT_EXAMPLES.single), {
    multiple: false,
    items: [{ header: 'Deploy', selectedLabels: ['host_c'] }],
  });
  assert.deepEqual(parsePentacleQuestionAnswerText(QUESTION_ANSWER_FORMAT_EXAMPLES.multiSelect), {
    multiple: false,
    items: [{ header: 'Toppings', selectedLabels: ['Cheese', 'Mushrooms'] }],
  });
  assert.deepEqual(parsePentacleQuestionAnswerText(QUESTION_ANSWER_FORMAT_EXAMPLES.optionWithNote), {
    multiple: false,
    items: [{ header: 'Deploy', selectedLabels: ['workstation'], note: 'Use the orchestrator.' }],
  });
});

test('returns null for input without the answer sentinel', () => {
  assert.equal(parsePentacleQuestionAnswerText('just a normal message'), null);
  assert.equal(parsePentacleQuestionAnswerText('Q1 (Deploy): host_c'), null);
  assert.equal(parsePentacleQuestionAnswerText(''), null);
});

test('returns null for a non-string input', () => {
  // The renderer feeds untyped event text; guard against non-string at runtime.
  assert.equal(parsePentacleQuestionAnswerText(undefined as unknown as string), null);
  assert.equal(parsePentacleQuestionAnswerText(null as unknown as string), null);
});

test('returns null for a malformed answer body (sentinel but no question block)', () => {
  assert.equal(parsePentacleQuestionAnswerText('Answering your question:\n\nnot a question line'), null);
  assert.equal(parsePentacleQuestionAnswerText('Answering your question:\n\n'), null);
});
