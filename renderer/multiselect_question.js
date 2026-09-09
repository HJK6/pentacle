'use strict';

// Renders a top-level multi-select question as checkbox-style options + a Submit button, batching the
// multi-answer behind a single submit. Extracted from app.js renderSlotChat so
// the multiSelect behavior has deterministic jsdom coverage (and so the cosmic
// theme layer can restyle one well-scoped surface). The single-select +
// multi-question rendering paths stay inline in app.js, unchanged.
//
// Public question contract:
//   - non-meta options are the selectable checkboxes — the selectable-options rule
//     excludes meta selector built-ins ("Type something") from a multiSelect set;
//   - multiple options may be selected at once (checkbox, not radio);
//   - the answer is the batched daemon RPC payload public answer serializer
//     emits for a multiSelect item:
//       { answers: [{ question_index: 0, option_indices: [...] }] }
//
// Selection persists across the host's ~1Hz question re-render via the shared
// `drafts` map (state.questionDrafts) keyed by the question signature, exactly
// like the multi-question path; once submitted the question is locked via
// `answeredSig` (state.answeredQuestionSig) until the daemon clears/changes it.

function renderMultiSelectQuestion(opts) {
  const {
    container,
    doc = (typeof document !== 'undefined' ? document : null),
    question,
    streamId,
    questionSig,
    alreadyAnswered = false,
    drafts = {},
    answeredSig = {},
    onAnswer,
  } = opts || {};
  if (!container || !doc || !question) return;

  // Persist selection across re-renders: (re)seed the draft when the question
  // signature changes, otherwise reuse the existing in-flight selection.
  if (!streamId || !drafts[streamId] || drafts[streamId].sig !== questionSig) {
    if (streamId) drafts[streamId] = { sig: questionSig, answers: {} };
  }
  const draft = streamId ? drafts[streamId] : { sig: questionSig, answers: {} };
  if (!draft.answers[0] || !Array.isArray(draft.answers[0].optionIndices)) {
    draft.answers[0] = { optionIndices: [] };
  }
  const selected = () => draft.answers[0].optionIndices;

  const promptEl = doc.createElement('div');
  promptEl.className = 'slot-chat-question-prompt';
  const promptText = question.header
    ? `${question.header}: ${question.prompt || ''}`.trim().replace(/:\s*$/, '')
    : (question.prompt || '');
  promptEl.textContent = promptText;
  container.appendChild(promptEl);

  const optionsEl = doc.createElement('div');
  optionsEl.className = 'slot-chat-question-options is-multiselect';
  // Non-meta options are the selectable checkboxes (matches public's
  // selectableOptions, which excludes meta options from a multiSelect set).
  const selectable = (Array.isArray(question.options) ? question.options : []).filter((opt) => !opt.meta);

  const refreshSubmit = () => {
    const submit = container.querySelector('.slot-chat-question-submit');
    if (submit) submit.disabled = alreadyAnswered || selected().length === 0;
  };

  for (const opt of selectable) {
    const index = Number(opt.index);
    if (!Number.isFinite(index)) continue;
    const btn = doc.createElement('button');
    btn.type = 'button';
    btn.className = 'slot-chat-question-option is-checkbox';
    btn.setAttribute('role', 'checkbox');
    const initiallySelected = selected().includes(index);
    btn.setAttribute('aria-checked', initiallySelected ? 'true' : 'false');
    if (initiallySelected) btn.classList.add('is-selected');
    btn.dataset.option = String(index);
    btn.disabled = alreadyAnswered;
    const labelEl = doc.createElement('span');
    labelEl.className = 'slot-chat-question-option-label';
    labelEl.textContent = opt.label || String(index);
    btn.appendChild(labelEl);
    if (opt.description) {
      const descEl = doc.createElement('span');
      descEl.className = 'slot-chat-question-option-desc';
      descEl.textContent = opt.description;
      btn.appendChild(descEl);
    }
    btn.addEventListener('click', () => {
      // Toggle this option in the multi-select set (multiple may be on at once).
      const current = selected();
      const at = current.indexOf(index);
      if (at >= 0) current.splice(at, 1);
      else { current.push(index); current.sort((a, b) => a - b); }
      const nowSelected = current.includes(index);
      btn.classList.toggle('is-selected', nowSelected);
      btn.setAttribute('aria-checked', nowSelected ? 'true' : 'false');
      refreshSubmit();
    });
    optionsEl.appendChild(btn);
  }
  container.appendChild(optionsEl);

  const actionsEl = doc.createElement('div');
  actionsEl.className = 'slot-chat-question-actions';
  const submitBtn = doc.createElement('button');
  submitBtn.type = 'button';
  submitBtn.className = 'slot-chat-question-submit';
  submitBtn.textContent = 'Submit';
  submitBtn.disabled = alreadyAnswered || selected().length === 0;
  actionsEl.appendChild(submitBtn);
  container.appendChild(actionsEl);

  submitBtn.addEventListener('click', () => {
    const optionIndices = selected()
      .map((value) => Number(value))
      .filter((value) => Number.isFinite(value) && value > 0);
    if (!optionIndices.length) return;
    // Lock the card until the daemon clears/changes the question, preventing a
    // stray second submit on a ~1Hz re-render (single-select does the same).
    if (streamId) answeredSig[streamId] = questionSig;
    container.querySelectorAll('button').forEach((el) => { el.disabled = true; });
    if (typeof onAnswer === 'function') {
      onAnswer({ answers: [{ question_index: 0, option_indices: optionIndices }] });
    }
  });
}

module.exports = { renderMultiSelectQuestion };
