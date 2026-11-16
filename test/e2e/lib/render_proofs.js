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
  wrapSourceEvent, assertFreshWrapFixture, measureWrapSource, buildWrapSourceProofScript, pasteFixture,
};

// Source identity, never a count of all cards or a model's promised echo.
function wrapSourceEvent(events, streamId, sourceEventId) {
  const rows = events.filter(row => row.stream_id === streamId && row.kind === 'ASSIST_TEXT'
    && String(row.event_id || row.message_id || row.daemon_seq) === String(sourceEventId));
  if (rows.length !== 1) throw new Error('fixture needs exactly one named source event');
  const text = String(rows[0].text || '');
  const source = { streamId, sourceEventId,
    longUrl: text.match(/https:\/\/example\.com\/[a-z0-9]+/i)?.[0],
    word: text.match(/(?:^|\n)(LongWord[a-z0-9]+)/i)?.[1],
    inline: text.match(/`([^`\n]+)`/)?.[1],
    href: text.match(/\[[^\]]+\]\((https:\/\/example\.com\/[a-z0-9]+)\)/i)?.[1] };
  for (const key of ['longUrl', 'word', 'inline', 'href']) {
    if (!source[key] || source[key].length < 300) throw new Error(`fixture source ${key} is missing or shorter than 300 characters`);
  }
  return source;
}

function assertFreshWrapFixture(identity, events) {
  if (!identity || !['rehearsal', 'live'].includes(identity.phase) || typeof identity.fixtureId !== 'string' || !identity.fixtureId.trim()
      || typeof identity.streamId !== 'string' || !identity.streamId.includes(':')) {
    throw new Error('fixture needs explicit phase, fixtureId and streamId');
  }
  if (identity.phase === 'live' && (!identity.rehearsalStreamId || !identity.rehearsalFixtureId
      || identity.streamId === identity.rehearsalStreamId || identity.fixtureId === identity.rehearsalFixtureId)) {
    throw new Error('live and rehearsal fixtures must have distinct chat and fixture identities');
  }
  if (events.some(row => row.stream_id === identity.streamId
      && (['USER', 'ASSIST_TEXT', 'TOOL_CALL', 'TOOL_RESULT'].includes(row.kind) || row.attachments?.length))) {
    throw new Error('fixture chat must be fresh before mutation');
  }
}

// Runs unchanged in the browser for both the seeded gate and served proof.
function measureWrapSource(root, source) {
  if (!root) throw new Error('fixture transcript root missing');
  const cards = [...root.querySelectorAll('.slot-chat-assistant-card')].filter(card =>
    [...card.querySelectorAll('.slot-chat-md-icode')].some(node => node.textContent === source.inline)
      && card.textContent.includes(source.longUrl) && card.textContent.includes(source.word));
  if (cards.length !== 1) throw new Error('fixture source must identify exactly one rendered card');
  const card = cards[0], box = card.getBoundingClientRect(), selection = window.getSelection();
  const records = [];
  try {
    for (const kind of ['longUrl', 'word', 'inline']) {
      let node;
      if (kind === 'inline') node = [...card.querySelectorAll('.slot-chat-md-icode')].find(n => n.textContent === source.inline)?.firstChild;
      else {
        const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
          if (walker.currentNode.textContent === source[kind] && !walker.currentNode.parentElement.closest('pre,a,button,.slot-chat-md-icode')) {
            node = walker.currentNode; break;
          }
        }
      }
      if (!node) throw new Error(`missing exact source text ${kind}`);
      const range = document.createRange(); range.selectNodeContents(node);
      const rects = [...range.getClientRects()];
      selection.removeAllRanges(); selection.addRange(range);
      records.push({ kind, chars: node.textContent.length, lines: rects.length,
        fits: rects.every(rect => rect.left >= box.left - 1 && rect.right <= box.right + 1),
        copyExact: selection.toString() === source[kind] });
    }
    return { records, hrefExact: [...card.querySelectorAll('a')].some(a => a.getAttribute('href') === source.href && a.href === source.href),
      pageFits: document.documentElement.scrollWidth <= innerWidth + 1,
      transcriptFits: root.scrollWidth <= root.clientWidth + 1 };
  } finally { selection.removeAllRanges(); }
}

function buildWrapSourceProofScript(selector, source) {
  return `(${measureWrapSource.toString()})(document.querySelector(${JSON.stringify(selector)}),${JSON.stringify(source)})`;
}


// This body executes in the browser and uses actual File/DataTransfer data.
function pasteFixture(options) {
  const e = document.querySelector('#cell-0 .slot-chat-compose-input');
  e.focus();
  if (options.draft !== undefined) {
    e.value = options.draft; e.dispatchEvent(new Event('input', { bubbles: true }));
    e.setSelectionRange(...(options.selection || [e.value.length, e.value.length]));
  }
  const canvas = document.createElement('canvas'); canvas.width = 3; canvas.height = 2;
  canvas.getContext('2d').fillRect(0, 0, 3, 2);
  const files = (options.mimes || ['image/png']).map((mime, i) => {
    let bytes = Uint8Array.from(atob(canvas.toDataURL(mime === 'image/jpeg' ? mime : 'image/png').split(',')[1]), c => c.charCodeAt(0));
    if (options.invalid === 'empty') bytes = new Uint8Array();
    if (options.invalid === 'corrupt') bytes = new Uint8Array([1, 2, 3]);
    if (options.size) { const padded = new Uint8Array(options.size); padded.set(bytes); bytes = padded; }
    return new File([bytes], `clipboard-${i}.${mime === 'image/jpeg' ? 'jpg' : 'png'}`, { type: mime });
  });
  const dt = new DataTransfer(); files.forEach(f => dt.items.add(f));
  if (options.text) dt.setData('text/plain', options.text);
  dt.setData('text/html', '<b>never insert HTML</b>');
  let clipboardData = dt;
  if (options.shape) clipboardData = {
    items: options.shape === 'files' ? [] : files.map(f => ({ kind: 'file', type: f.type,
      getAsFile: () => options.shape === 'null' ? null : f })),
    files: options.shape === 'items' || options.shape === 'null' ? [] : files,
    getData: type => dt.getData(type),
  };
  const event = new ClipboardEvent('paste', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'clipboardData', { value: clipboardData });
  let inputs = 0; const count = () => { inputs++; }; e.addEventListener('input', count);
  e.dispatchEvent(event); e.removeEventListener('input', count);
  return { prevented: event.defaultPrevented, inputs, caret: e.selectionStart,
    sourcePngBase64: canvas.toDataURL().split(',')[1],
    textCorrect: options.expected === undefined || e.value === options.expected,
    files: files.map(f => ({ mime: f.type, bytes: f.size })) };
}
