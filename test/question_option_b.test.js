'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { createRequire } = require('node:module');
const Module = require('node:module');
const esbuild = require('esbuild');
const { JSDOM } = require('jsdom');

const root = path.join(__dirname, '..');
const rendererRequire = createRequire(path.join(root, 'renderer', 'app.js'));
const { renderQuestionOptionB } = rendererRequire('./question_option_b');
const { buildPentacleQuestionAnswerText } = loadSharedChatCore();

function freshDoc() {
  const dom = new JSDOM('<!DOCTYPE html><html><body><div id="q"></div></body></html>');
  return dom.window.document;
}

function loadSharedChatCore() {
  const bundled = esbuild.buildSync({
    entryPoints: [path.join(root, 'pentacle-chat-core', 'src', 'index.ts')],
    bundle: true,
    platform: 'node',
    format: 'cjs',
    write: false,
    logLevel: 'silent',
  });
  const mod = new Module(path.join(root, 'pentacle-chat-core', 'src', 'index.ts'), module);
  mod.filename = path.join(root, 'pentacle-chat-core', 'src', 'index.ts');
  mod.paths = Module._nodeModulePaths(path.join(root, 'pentacle-chat-core'));
  mod._compile(bundled.outputFiles[0].text, mod.filename);
  return mod.exports;
}

function renderInto(doc, {
  question,
  onSubmit,
  onCancel,
  drafts = {},
  answeredSig = {},
  alreadyAnswered = false,
  allowFreeText = true,
  submitRequiresSelection = false,
  showCancel = true,
} = {}) {
  const container = doc.getElementById('q');
  renderQuestionOptionB({
    container,
    doc,
    question,
    streamId: 'hostc:claude-hostc-1',
    questionSig: 'sig-1',
    alreadyAnswered,
    drafts,
    answeredSig,
    buildAnswerText: buildPentacleQuestionAnswerText,
    onSubmit,
    onCancel,
    allowFreeText,
    submitRequiresSelection,
    showCancel,
  });
  return container;
}

function singleQuestion() {
  return {
    question_key: 'qkey-single',
    header: 'Pick',
    prompt: 'Pick a number',
    options: [
      { index: 1, label: 'One' },
      { index: 2, label: 'Two' },
      { index: 3, label: 'Type something.', meta: true },
    ],
  };
}

function multiSelectQuestion() {
  return {
    question_key: 'qkey-ms',
    header: 'Toppings',
    prompt: 'Which toppings?',
    multiSelect: true,
    options: [
      { index: 1, label: 'Cheese' },
      { index: 2, label: 'Mushroom' },
      { index: 3, label: 'Type something', meta: true },
    ],
  };
}

function multiQuestion() {
  return {
    question_key: 'qkey-multi',
    multi: true,
    prompt: 'Answer these',
    options: [],
    questions: [
      {
        index: 0,
        header: 'Host',
        prompt: 'Pick host',
        options: [
          { index: 1, label: 'hostc' },
          { index: 2, label: 'hostd' },
          { index: 3, label: 'Type something', meta: true },
        ],
      },
      {
        index: 1,
        header: 'Flags',
        prompt: 'Pick flags',
        multiSelect: true,
        options: [
          { index: 1, label: 'Fast' },
          { index: 2, label: 'Careful' },
          { index: 3, label: 'Type something', meta: true },
        ],
      },
    ],
  };
}

test('single-select renders parsed options plus always free text, note, and Cancel; meta is excluded', () => {
  const doc = freshDoc();
  const container = renderInto(doc, { question: singleQuestion() });
  assert.deepEqual([...container.querySelectorAll('.slot-chat-question-option[data-option]')].map((b) => b.textContent), ['One', 'Two']);
  assert.equal(container.querySelectorAll('.slot-chat-question-option.is-meta').length, 0);
  assert.ok(container.querySelector('.slot-chat-question-freetext'));
  assert.ok(container.querySelector('.slot-chat-question-note'));
  assert.equal(container.querySelector('.slot-chat-question-note').hidden, true);
  assert.ok(container.querySelector('.slot-chat-question-cancel'));
  assert.equal(container.querySelector('.slot-chat-question-submit').disabled, true);
});

test('single-select submit sends exact builder text through dismiss', async () => {
  const doc = freshDoc();
  let submitted = null;
  const container = renderInto(doc, { question: singleQuestion(), onSubmit: async (text) => { submitted = text; } });
  container.querySelector('[data-option="2"]').click();
  assert.equal(container.querySelector('.slot-chat-question-note').hidden, false);
  container.querySelector('.slot-chat-question-note').value = 'ship it';
  container.querySelector('.slot-chat-question-note').dispatchEvent(new doc.defaultView.Event('input', { bubbles: true }));
  container.querySelector('.slot-chat-question-submit').click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(submitted, 'Answering your question:\n\nQ1 (Pick): Two\nnote (Q1): ship it');
});

test('structured submit exposes option values and can require a selection without free text', async () => {
  const doc = freshDoc();
  let submitted = null;
  const question = {
    question_key: 'qkey-durable',
    header: 'Pick',
    prompt: 'Pick a value',
    options: [
      { index: 1, label: 'Alpha', value: 'alpha' },
      { index: 2, label: 'Beta', value: 'beta' },
    ],
  };
  const container = renderInto(doc, {
    question,
    allowFreeText: false,
    submitRequiresSelection: true,
    showCancel: false,
    onSubmit: async (_text, detail) => { submitted = detail; },
  });
  assert.equal(container.querySelector('.slot-chat-question-freetext'), null);
  assert.equal(container.querySelector('.slot-chat-question-cancel'), null);
  const note = container.querySelector('.slot-chat-question-note');
  note.value = 'context';
  note.dispatchEvent(new doc.defaultView.Event('input', { bubbles: true }));
  assert.equal(container.querySelector('.slot-chat-question-submit').disabled, true);
  container.querySelector('[data-option="2"]').click();
  container.querySelector('.slot-chat-question-submit').click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(submitted.answers[0].selectedOptionValues, ['beta']);
  assert.equal(submitted.answers[0].note, 'context');
});

test('custom text choice submit stands alone and replaces selections', async () => {
  const doc = freshDoc();
  const submissions = [];
  const question = {
    question_key: 'qkey-custom',
    header: 'Pick',
    prompt: 'Pick a value',
    customText: true,
    options: [
      { index: 1, label: 'Alpha', value: 'alpha', description: 'First option' },
      { index: 2, label: 'Beta', value: 'beta' },
    ],
  };
  const container = renderInto(doc, {
    question,
    allowFreeText: true,
    submitRequiresSelection: false,
    showCancel: false,
    onSubmit: async (_text, detail) => { submissions.push(detail); },
  });

  assert.equal(container.querySelector('.slot-chat-question-option-desc').textContent, 'First option');
  const free = container.querySelector('.slot-chat-question-freetext');
  free.value = 'custom answer';
  free.dispatchEvent(new doc.defaultView.Event('input', { bubbles: true }));
  container.querySelector('.slot-chat-question-submit').click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(submissions[0].answers[0].customText, 'custom answer');
  assert.equal(submissions[0].answers[0].selectedOptionValues, undefined);

  const doc2 = freshDoc();
  const container2 = renderInto(doc2, {
    question,
    allowFreeText: true,
    submitRequiresSelection: false,
    showCancel: false,
    onSubmit: async (_text, detail) => { submissions.push(detail); },
  });
  container2.querySelector('[data-option="1"]').click();
  const free2 = container2.querySelector('.slot-chat-question-freetext');
  free2.value = 'custom with selection';
  free2.dispatchEvent(new doc2.defaultView.Event('input', { bubbles: true }));
  container2.querySelector('.slot-chat-question-submit').click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(submissions[1].answers[0].selectedOptionValues, undefined);
  assert.equal(submissions[1].answers[0].customText, 'custom with selection');
});

test('multiSelect renders checkboxes, free text, note, Cancel, and excludes meta options', async () => {
  const doc = freshDoc();
  let submitted = null;
  const container = renderInto(doc, { question: multiSelectQuestion(), onSubmit: async (text) => { submitted = text; } });
  const options = [...container.querySelectorAll('.slot-chat-question-option.is-checkbox[data-option]')];
  assert.deepEqual(options.map((b) => Number(b.dataset.option)), [1, 2]);
  assert.ok(options.every((b) => b.getAttribute('role') === 'checkbox'));
  assert.ok(container.querySelector('.slot-chat-question-freetext'));
  assert.ok(container.querySelector('.slot-chat-question-note'));
  assert.ok(container.querySelector('.slot-chat-question-cancel'));
  options[0].click();
  options[1].click();
  container.querySelector('.slot-chat-question-submit').click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(submitted, 'Answering your question:\n\nQ1 (Toppings):\n- Cheese\n- Mushroom');
});

test('free text is always available and submits as builder free-text answer', async () => {
  const doc = freshDoc();
  let submitted = null;
  const container = renderInto(doc, { question: singleQuestion(), onSubmit: async (text) => { submitted = text; } });
  const free = container.querySelector('.slot-chat-question-freetext');
  free.value = 'Something else\nwith detail';
  free.dispatchEvent(new doc.defaultView.Event('input', { bubbles: true }));
  container.querySelector('.slot-chat-question-submit').click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(submitted, 'Answering your question:\n\nQ1 (Pick):\nSomething else\nwith detail');
});

test('multi-question renders each card with free text, note, Cancel, no meta leak, and builder text', async () => {
  const doc = freshDoc();
  let submitted = null;
  const container = renderInto(doc, { question: multiQuestion(), onSubmit: async (text) => { submitted = text; } });
  assert.equal(container.querySelectorAll('.slot-chat-question-card').length, 2);
  assert.equal(container.querySelectorAll('.slot-chat-question-freetext').length, 2);
  assert.equal(container.querySelectorAll('.slot-chat-question-note').length, 2);
  assert.equal(container.querySelectorAll('.slot-chat-question-option.is-meta').length, 0);
  container.querySelector('.slot-chat-question-card[data-question-index="0"] [data-option="1"]').click();
  container.querySelector('.slot-chat-question-card[data-question-index="1"] [data-option="1"]').click();
  container.querySelector('.slot-chat-question-card[data-question-index="1"] [data-option="2"]').click();
  container.querySelector('.slot-chat-question-submit').click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(submitted, 'Answering your questions:\n\nQ1 (Host): hostc\n\nQ2 (Flags):\n- Fast\n- Careful');
});

test('Cancel-only settles without text', async () => {
  const doc = freshDoc();
  let cancelled = false;
  let submitted = false;
  const container = renderInto(doc, {
    question: singleQuestion(),
    onSubmit: async () => { submitted = true; },
    onCancel: async () => { cancelled = true; },
  });
  container.querySelector('.slot-chat-question-cancel').click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(cancelled, true);
  assert.equal(submitted, false);
});

test('pager controls render and call page handlers', () => {
  const doc = freshDoc();
  let prev = 0;
  let next = 0;
  const container = doc.getElementById('q');
  renderQuestionOptionB({
    container,
    doc,
    question: singleQuestion(),
    streamId: 'hostc:claude-hostc-1',
    questionSig: 'sig-1',
    drafts: {},
    answeredSig: {},
    buildAnswerText: buildPentacleQuestionAnswerText,
    pagerLabel: '1/2',
    canPagePrev: false,
    canPageNext: true,
    onPagePrev: () => { prev += 1; },
    onPageNext: () => { next += 1; },
    onSubmit: async () => {},
    onCancel: async () => {},
  });
  assert.equal(container.querySelector('.slot-chat-question-page-label').textContent, '1/2');
  container.querySelectorAll('.slot-chat-question-page')[0].click();
  container.querySelectorAll('.slot-chat-question-page')[1].click();
  assert.equal(prev, 0);
  assert.equal(next, 1);
});


test('mobile parity: custom answers replace selections and selection bounds gate submission', () => {
  const doc = freshDoc();
  const drafts = {};
  const question = { ...multiSelectQuestion(), customText: true, min_select: 2, max_select: 2 };
  const container = renderInto(doc, { question, drafts });
  container.querySelector('[data-option="1"]').click();
  assert.equal(container.querySelector('.slot-chat-question-submit').disabled, true, 'minimum is enforced');
  container.querySelector('[data-option="2"]').click();
  assert.equal(container.querySelector('.slot-chat-question-submit').disabled, false);
  const free = container.querySelector('.slot-chat-question-freetext');
  free.value = 'My custom choice';
  free.dispatchEvent(new doc.defaultView.Event('input'));
  assert.equal(container.querySelectorAll('.slot-chat-question-option.is-selected').length, 0, 'custom is exclusive');
  const { buildAnswersForQuestion } = rendererRequire('./question_option_b');
  const answers = buildAnswersForQuestion(question, drafts['hostc:claude-hostc-1']);
  assert.equal(answers[0].customText, 'My custom choice');
  assert.equal(answers[0].selectedOptionValues, undefined);
});

test('mobile parity: free_text false does not expose a custom answer control', () => {
  const doc = freshDoc();
  const container = renderInto(doc, { question: { ...singleQuestion(), free_text: false } });
  assert.equal(container.querySelector('.slot-chat-question-freetext'), null);
});
