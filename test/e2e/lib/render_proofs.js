'use strict';

function normalizeQuestionOptionLabel(value) {
  return String(value ?? '').replace(/\s+/g, ' ').trim();
}

function isPaneNoiseLabel(value) {
  const text = normalizeQuestionOptionLabel(value);
  return /[\u2500-\u257f]|\x1b\[[0-?]*[ -/]*[@-~]|\[[0-9;]{1,8}m|^\s*[>\u203a\u276f]\s|^\s*(reconnect(?: ok)?|connected|connecting|disconnected)\s*$/i.test(text);
}

function questionItemsForLabelProof(question) {
  if (question && Array.isArray(question.questions) && question.questions.length) {
    return question.questions.map((item, pos) => ({
      ...item,
      _proofQuestionIndex: Number.isFinite(Number(item.index)) ? Number(item.index) : pos,
    }));
  }
  return [{
    _proofQuestionIndex: 0,
    options: Array.isArray(question?.options) ? question.options : [],
  }];
}

function expectedQuestionOptionLabels(question) {
  const out = [];
  for (const item of questionItemsForLabelProof(question)) {
    const questionIndex = String(item._proofQuestionIndex);
    const options = Array.isArray(item.options) ? item.options : [];
    for (const opt of options) {
      if (opt?.meta) continue;
      const optionIndex = Number(opt?.index);
      if (!Number.isFinite(optionIndex)) continue;
      out.push({
        key: `${questionIndex}:${optionIndex}`,
        questionIndex,
        optionIndex: String(optionIndex),
        label: normalizeQuestionOptionLabel(opt.label || String(optionIndex)),
      });
    }
  }
  return out;
}

function renderedQuestionOptionLabels(root) {
  if (!root || typeof root.querySelectorAll !== 'function') return [];
  return Array.from(root.querySelectorAll('.slot-chat-question-option[data-option]')).map((button) => {
    const labelEl = button.querySelector?.('.slot-chat-question-option-label');
    const card = button.closest?.('.slot-chat-question-card[data-question-index]');
    const questionIndex = button.getAttribute('data-question-index')
      || card?.getAttribute('data-question-index')
      || '0';
    const optionIndex = button.getAttribute('data-option') || '';
    const label = normalizeQuestionOptionLabel(labelEl ? labelEl.textContent : button.textContent);
    return {
      key: `${questionIndex}:${optionIndex}`,
      questionIndex,
      optionIndex,
      label,
    };
  });
}

function compareQuestionOptionLabels(expected, rendered) {
  const expectedRows = Array.isArray(expected) ? expected : [];
  const renderedRows = Array.isArray(rendered) ? rendered : [];
  const expectedByKey = new Map(expectedRows.map((row) => [row.key, row]));
  const renderedByKey = new Map(renderedRows.map((row) => [row.key, row]));
  const mismatches = [];
  const noiseLabels = [];

  for (const row of [...expectedRows, ...renderedRows]) {
    if (isPaneNoiseLabel(row.label)) noiseLabels.push(row);
  }

  for (const row of expectedRows) {
    const actual = renderedByKey.get(row.key);
    if (!actual) {
      mismatches.push({ key: row.key, expected: row.label, actual: null });
    } else if (normalizeQuestionOptionLabel(actual.label) !== normalizeQuestionOptionLabel(row.label)) {
      mismatches.push({ key: row.key, expected: row.label, actual: actual.label });
    }
  }

  for (const row of renderedRows) {
    if (!expectedByKey.has(row.key)) {
      mismatches.push({ key: row.key, expected: null, actual: row.label });
    }
  }

  return {
    ok: expectedRows.length > 0
      && expectedRows.length === renderedRows.length
      && mismatches.length === 0
      && noiseLabels.length === 0,
    expected: expectedRows,
    rendered: renderedRows,
    mismatches,
    noiseLabels,
  };
}

function buildQuestionLabelProofScript(cellSelector, streamIdLiteral) {
  return `(() => {
    const normalizeQuestionOptionLabel = ${normalizeQuestionOptionLabel.toString()};
    const isPaneNoiseLabel = ${isPaneNoiseLabel.toString()};
    const questionItemsForLabelProof = ${questionItemsForLabelProof.toString()};
    const expectedQuestionOptionLabels = ${expectedQuestionOptionLabels.toString()};
    const renderedQuestionOptionLabels = ${renderedQuestionOptionLabels.toString()};
    const compareQuestionOptionLabels = ${compareQuestionOptionLabels.toString()};
    const root = document.querySelector(${JSON.stringify(cellSelector)});
    const question = window.PentacleChatStore.getQuestion(${streamIdLiteral});
    return JSON.stringify(compareQuestionOptionLabels(
      expectedQuestionOptionLabels(question),
      renderedQuestionOptionLabels(root),
    ));
  })()`;
}

function countTranscriptRows(root) {
  if (!root || typeof root.querySelectorAll !== 'function') return 0;
  return root.querySelectorAll('.slot-chat-row').length;
}

function buildTranscriptRowProofScript(cellSelector) {
  return `(() => {
    const root = document.querySelector(${JSON.stringify(cellSelector)});
    const countTranscriptRows = ${countTranscriptRows.toString()};
    const n = countTranscriptRows(root);
    return n > 0 ? n : false;
  })()`;
}

module.exports = {
  normalizeQuestionOptionLabel,
  isPaneNoiseLabel,
  expectedQuestionOptionLabels,
  renderedQuestionOptionLabels,
  compareQuestionOptionLabels,
  buildQuestionLabelProofScript,
  countTranscriptRows,
  buildTranscriptRowProofScript,
};
